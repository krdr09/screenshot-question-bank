from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.config import Settings
from app.database import Database
from app.cropping.questions import CropBounds, crop_question_image
from app.models import CandidateImagePart, ImportBatch, QuestionCandidate, SourceImage
from app.ocr.base import OcrEngine
from app.parsing.questions import ParsedQuestion, parse_ocr_page
from app.services.import_batch_lock import import_batch_lock
from app.services.relationships.pipeline import analyze_batch_relationships
from app.services.backups.errors import BackupBusyError
from app.services.backups.maintenance import MaintenanceCoordinator
from app.services.review.actions import _apply_parsed
from app.services.review.editor import update_batch_review_state
from app.services.review.snapshots import capture_candidate, record_action


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PersistentOcrWorker:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        engine: OcrEngine,
        maintenance: MaintenanceCoordinator | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.engine = engine
        self.maintenance = maintenance or MaintenanceCoordinator()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def enqueue(self, batch_id: str) -> None:
        with import_batch_lock(batch_id):
            with self.database.session_factory() as session:
                batch = session.get(ImportBatch, batch_id)
                if batch is None:
                    raise ValueError("导入任务不存在")
                if not batch.images:
                    raise ValueError("导入任务没有可处理图片")
                if batch.status != "uploading":
                    raise ValueError(
                        f"导入任务当前状态不允许处理：{batch.status}"
                    )
                batch.status = "queued"
                batch.cancel_requested = False
                batch.current_file = None
                batch.ocr_device = self.engine.device
                session.commit()
        self._wake.set()

    def request_cancel(self, batch_id: str) -> None:
        with self.database.session_factory() as session:
            batch = session.get(ImportBatch, batch_id)
            if batch is None:
                raise ValueError("导入任务不存在")
            batch.cancel_requested = True
            session.commit()
        self._wake.set()

    def recover_interrupted_jobs(self) -> int:
        with self.database.session_factory() as session:
            batches = list(
                session.scalars(
                    select(ImportBatch).where(ImportBatch.status == "processing")
                )
            )
            for batch in batches:
                batch.status = "queued"
                batch.current_file = None
                for image in batch.images:
                    if image.ocr_status == "processing":
                        image.ocr_status = "pending"
            session.commit()
            return len(batches)

    def run_next(self) -> str | None:
        try:
            with self.maintenance.activity("OCR 处理"):
                return self._run_next_with_maintenance()
        except BackupBusyError:
            return None

    def _run_next_with_maintenance(self) -> str | None:
        with self.database.session_factory() as session:
            batch = session.scalar(
                select(ImportBatch)
                .where(ImportBatch.status == "queued")
                .order_by(ImportBatch.created_at, ImportBatch.id)
                .limit(1)
            )
            if batch is None:
                return None
            batch_id = batch.id
            if batch.cancel_requested:
                batch.status = "cancelled"
                batch.current_file = None
                session.commit()
                return batch_id
            batch.status = "processing"
            batch.ocr_device = self.engine.device
            session.commit()

        self._process_batch(batch_id)
        return batch_id

    def _process_batch(self, batch_id: str) -> None:
        analyze_relationships = False
        with self.database.session_factory() as session:
            batch = session.get(ImportBatch, batch_id)
            if batch is None:
                return
            duplicates = list(
                session.scalars(
                    select(SourceImage).where(
                        SourceImage.batch_id == batch_id,
                        SourceImage.duplicate_of_id.is_not(None),
                        SourceImage.ocr_status.not_in(["duplicate", "completed"]),
                    )
                )
            )
            for duplicate in duplicates:
                duplicate.ocr_status = "duplicate"
                duplicate.review_flag = "duplicate"
                duplicate.processed_at = utc_now()
                batch.processed_count += 1
            session.commit()
            image_ids = list(
                session.scalars(
                    select(SourceImage.id)
                    .where(
                        SourceImage.batch_id == batch_id,
                        SourceImage.duplicate_of_id.is_(None),
                        SourceImage.ocr_status != "completed",
                    )
                    .order_by(SourceImage.order_index)
                )
            )

        for image_id in image_ids:
            with self.database.session_factory() as session:
                batch = session.get(ImportBatch, batch_id)
                image = session.get(SourceImage, image_id)
                if batch is None or image is None:
                    return
                if batch.cancel_requested:
                    batch.status = "cancelled"
                    batch.current_file = None
                    session.commit()
                    return
                batch.current_file = image.original_name
                image.ocr_status = "processing"
                session.commit()
                image_path = self.settings.data_dir / Path(image.relative_path)

            written_crops: list[Path] = []
            try:
                result = self.engine.recognize(image_path)
            except Exception as error:
                self._record_image_error(batch_id, image_id, error)
                continue

            try:
                with self.database.session_factory() as session:
                    batch = session.get(ImportBatch, batch_id)
                    image = session.get(SourceImage, image_id)
                    if batch is None or image is None:
                        return
                    image.ocr_status = "completed"
                    image.ocr_result_json = result.model_dump(mode="json")
                    image.ocr_text = result.full_text
                    image.ocr_error = None
                    image.processed_at = utc_now()
                    batch.ocr_device = self.engine.device
                    parsed = parse_ocr_page(
                        result,
                        low_confidence_threshold=(
                            self.settings.ocr_confidence_threshold
                        ),
                    )
                    self._persist_candidates(
                        session,
                        batch,
                        image,
                        image_path,
                        parsed,
                        result.page_height,
                        written_crops,
                    )
                    batch.processed_count += 1
                    session.commit()
            except Exception as error:
                for crop_path in written_crops:
                    crop_path.unlink(missing_ok=True)
                self._record_image_error(batch_id, image_id, error)
                continue

        with self.database.session_factory() as session:
            batch = session.get(ImportBatch, batch_id)
            if batch is None:
                return
            batch.current_file = None
            if batch.cancel_requested:
                batch.status = "cancelled"
            elif batch.error_count == batch.processed_count and batch.error_count > 0:
                batch.status = "failed"
            elif batch.error_count > 0:
                batch.status = "partially_ready"
            else:
                analyze_relationships = True
            session.commit()
        if analyze_relationships:
            analyze_batch_relationships(self.database, self.settings, batch_id)

    def _record_image_error(
        self, batch_id: str, image_id: str, error: Exception
    ) -> None:
        with self.database.session_factory() as session:
            batch = session.get(ImportBatch, batch_id)
            image = session.get(SourceImage, image_id)
            if batch is None or image is None:
                return
            image.ocr_status = "failed"
            image.ocr_error = str(error)
            image.processed_at = utc_now()
            batch.processed_count += 1
            batch.error_count += 1
            batch.ocr_device = self.engine.device
            batch.error_log_json = [
                *batch.error_log_json,
                {"file": image.original_name, "message": str(error)},
            ]
            session.commit()

    def _persist_candidates(
        self,
        session,
        batch: ImportBatch,
        image: SourceImage,
        image_path: Path,
        parsed: list[ParsedQuestion],
        page_height: int,
        written_crops: list[Path],
    ) -> None:
        for question in parsed:
            candidate = QuestionCandidate(
                batch_id=batch.id,
                category_id=batch.category_id,
                source_image_id=image.id,
                source_display_number=question.source_display_number,
                question_type=question.question_type,
                stem=question.stem,
                options_json=[option.model_dump(mode="json") for option in question.options],
                correct_answer_json=question.correct_answer,
                source_correct_answer_raw=question.source_correct_answer_raw,
                true_label=question.true_label,
                false_label=question.false_label,
                explanation=question.explanation,
                raw_ocr_text=question.raw_ocr_text,
                normalized_ocr_text=question.normalized_ocr_text,
                ocr_confidence=question.ocr_confidence,
                parse_confidence=question.parse_confidence,
                display_mode=question.display_mode,
                anomaly_flags_json=[
                    flag.model_dump(mode="json") for flag in question.anomaly_flags
                ],
                status="review" if question.anomaly_flags else "ready",
            )
            session.add(candidate)
            session.flush()
            y_start = max(0, question.source_y_start - 8)
            y_end = (
                page_height
                if question.source_y_end == page_height
                else max(y_start + 1, question.source_y_end - 8)
            )
            crop_relative = Path("crops") / batch.id / f"{candidate.id}.png"
            crop_target = self.settings.data_dir / crop_relative
            crop_question_image(
                image_path,
                crop_target,
                CropBounds(
                    y_start=y_start,
                    y_end=y_end,
                    page_height=page_height,
                ),
            )
            written_crops.append(crop_target)
            session.add(
                CandidateImagePart(
                    candidate_id=candidate.id,
                    source_image_id=image.id,
                    crop_path=crop_relative.as_posix(),
                    source_y_start=y_start,
                    source_y_end=y_end,
                    part_index=0,
                )
            )
            batch.found_question_count += 1
            if question.anomaly_flags:
                batch.review_count += 1
            else:
                batch.high_confidence_count += 1

    def reprocess_image(self, image_id: str, audit_candidate_id: str | None = None) -> None:
        with self.maintenance.activity("OCR 重新识别"):
            self._reprocess_image_with_maintenance(image_id, audit_candidate_id)

    def _reprocess_image_with_maintenance(
        self, image_id: str, audit_candidate_id: str | None = None
    ) -> None:
        with self.database.session_factory() as session:
            image = session.get(SourceImage, image_id)
            if image is None:
                raise ValueError("来源图片不存在")
            image_path = self.settings.data_dir / Path(image.relative_path)
        result = self.engine.recognize(image_path)
        parsed = parse_ocr_page(
            result,
            low_confidence_threshold=self.settings.ocr_confidence_threshold,
        )
        if not parsed:
            raise ValueError("整图 OCR 未识别出题目")
        written_crops: list[Path] = []
        with self.database.session_factory() as session:
            image = session.get(SourceImage, image_id)
            if image is None:
                raise ValueError("来源图片不存在")
            batch = session.get(ImportBatch, image.batch_id)
            if batch is None:
                raise ValueError("导入批次不存在")
            existing = list(
                session.scalars(
                    select(QuestionCandidate)
                    .where(
                        QuestionCandidate.source_image_id == image_id,
                        QuestionCandidate.status.not_in(["deleted", "merged_source", "duplicate_merged", "replaced_by_ocr"]),
                    )
                    .order_by(QuestionCandidate.created_at, QuestionCandidate.id)
                )
            )
            before_ids = {candidate.id for candidate in existing}
            before_candidates = {
                candidate.id: capture_candidate(candidate) for candidate in existing
            }
            image_before = {
                "id": image.id,
                "ocr_status": image.ocr_status,
                "ocr_result_json": image.ocr_result_json,
                "ocr_text": image.ocr_text,
                "ocr_error": image.ocr_error,
            }
            for candidate, question in zip(existing, parsed):
                _apply_parsed(candidate, question)
                if candidate.image_parts:
                    part = candidate.image_parts[0]
                    y_start = max(0, question.source_y_start - 8)
                    y_end = result.page_height if question.source_y_end == result.page_height else max(y_start + 1, question.source_y_end - 8)
                    crop_question_image(image_path, self.settings.data_dir / part.crop_path, CropBounds(y_start=y_start, y_end=y_end, page_height=result.page_height))
                    part.source_y_start = y_start
                    part.source_y_end = y_end
            for candidate in existing[len(parsed):]:
                candidate.status = "replaced_by_ocr"
            if len(parsed) > len(existing):
                self._persist_candidates(
                    session,
                    batch,
                    image,
                    image_path,
                    parsed[len(existing):],
                    result.page_height,
                    written_crops,
                )
            image.ocr_status = "completed"
            image.ocr_result_json = result.model_dump(mode="json")
            image.ocr_text = result.full_text
            image.ocr_error = None
            image.processed_at = utc_now()
            batch.ocr_device = self.engine.device
            update_batch_review_state(session, batch.id)
            session.flush()
            current = list(
                session.scalars(
                    select(QuestionCandidate).where(
                        QuestionCandidate.source_image_id == image_id
                    )
                )
            )
            audit_candidate = next(
                (candidate for candidate in current if candidate.id == audit_candidate_id),
                existing[0] if existing else None,
            )
            if audit_candidate is not None:
                record_action(
                    session,
                    audit_candidate,
                    "rerun_source_ocr",
                    {
                        "candidates": before_candidates,
                        "created_candidate_ids": [
                            candidate.id for candidate in current if candidate.id not in before_ids
                        ],
                        "image": image_before,
                    },
                    {
                        "candidates": {
                            candidate.id: capture_candidate(candidate) for candidate in current
                        },
                        "image": {
                            "id": image.id,
                            "ocr_status": image.ocr_status,
                            "ocr_result_json": image.ocr_result_json,
                            "ocr_text": image.ocr_text,
                            "ocr_error": image.ocr_error,
                        },
                    },
                    target_type="source_image",
                    target_id=image.id,
                )
            session.commit()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.recover_interrupted_jobs()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="ocr-worker", daemon=True
        )
        self._thread.start()

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            if self.run_next() is None:
                self._wake.wait(timeout=0.5)
                self._wake.clear()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)
