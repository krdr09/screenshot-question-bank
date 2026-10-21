import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, BrokenBarrierError

import pytest
from PIL import Image
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Database
from app.models import CandidateImagePart, CandidateRelationship, Course, ImportBatch, QuestionCandidate, ReviewAction, SourceImage
from app.ocr.base import OcrBox, OcrPageResult, Point
from app.ocr.mock import MockOcrEngine
from app.services.review import actions, crop_files, snapshots
from app.services.review.actions import (
    mark_duplicate,
    merge_candidates,
    move_source_image,
    rerun_candidate_ocr,
    split_candidate,
    resolve_relationship,
)
from app.services.review.editor import (
    INACTIVE_CANDIDATE_STATUSES,
    ReviewValidationError,
    confirm_candidate,
    delete_candidate,
    ignore_candidate,
    undo_last_action,
    update_batch_review_state,
    update_candidate,
)
from app.services.review.relationship_state import ReviewRelationshipConflictError
from app.workers.ocr_worker import PersistentOcrWorker


def _box(text: str, top: int) -> OcrBox:
    return OcrBox(
        text=text,
        polygon=[Point(x=0, y=top), Point(x=100, y=top), Point(x=100, y=top + 12), Point(x=0, y=top + 12)],
        confidence=0.98,
    )


def _positioned_box(
    text: str,
    top: int,
    *,
    left: int = 0,
    right: int = 100,
    confidence: float = 0.98,
) -> OcrBox:
    return OcrBox(
        text=text,
        polygon=[
            Point(x=left, y=top),
            Point(x=right, y=top),
            Point(x=right, y=top + 10),
            Point(x=left, y=top + 10),
        ],
        confidence=confidence,
    )


def _repeated_title_page_result(
    *,
    question_type: str = "single_choice",
    include_widget: bool = False,
    display_number: str = "7",
    stem: str | None = None,
    confidence: float = 0.98,
    duplicate_match: bool = False,
) -> OcrPageResult:
    type_label = "单选题" if question_type == "single_choice" else "判断题"
    complete_stem = stem or (
        "示例命题完整内容"
        if question_type == "single_choice"
        else "示例判断命题完整内容"
    )

    def question_boxes(
        number: str,
        question_stem: str,
        top: int,
        *,
        with_widget: bool,
    ) -> list[OcrBox]:
        boxes = [
            _positioned_box(
                f"{number}.[{type_label}]", top, confidence=confidence
            ),
            _positioned_box(question_stem, top + 16, confidence=confidence),
        ]
        if with_widget:
            boxes.extend(
                [
                    _positioned_box(
                        "G",
                        top + 32,
                        left=96,
                        right=108,
                        confidence=confidence,
                    ),
                    _positioned_box(
                        "写笔记",
                        top + 44,
                        left=92,
                        right=116,
                        confidence=confidence,
                    ),
                ]
            )
        if question_type == "single_choice":
            boxes.extend(
                _positioned_box(text, option_top, confidence=confidence)
                for text, option_top in [
                    ("A. 甲", top + 60),
                    ("B. 乙", top + 76),
                    ("C. 丙", top + 92),
                    ("D. 丁", top + 108),
                    ("正确答案:D", top + 124),
                ]
            )
        else:
            boxes.append(
                _positioned_box("正确答案:错误", top + 60, confidence=confidence)
            )
        return boxes

    boxes = question_boxes(
        display_number,
        complete_stem,
        10,
        with_widget=include_widget,
    )
    boxes.extend(
        question_boxes(
            "8",
            "另一道完整题目",
            170,
            with_widget=False,
        )
    )
    if duplicate_match:
        boxes.extend(
            question_boxes(
                display_number,
                complete_stem,
                330,
                with_widget=include_widget,
            )
        )
    return OcrPageResult(
        page_width=120,
        page_height=500,
        boxes=boxes,
        raw_output={"fixture": "repeated-title-spatial-evidence"},
    )


def _result(stem: str = "重新识别题干", page_height: int = 100) -> OcrPageResult:
    return OcrPageResult(
        page_width=120,
        page_height=page_height,
        boxes=[
            _box("1.[单选题]", 10),
            _box(stem, 24),
            _box("A. 甲", 38),
            _box("B. 乙", 52),
            _box("正确答案: B", 66),
        ],
        raw_output={"mock": True},
    )


def _freeze_review_action_clock(
    monkeypatch: pytest.MonkeyPatch,
    instant: datetime,
) -> None:
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return instant.replace(tzinfo=None)
            return instant.astimezone(tz)

    monkeypatch.setattr(snapshots, "datetime", FrozenDateTime)


def _seed(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        auto_create_schema=True,
        start_background_worker=False,
        frontend_dist=tmp_path / "missing",
        _env_file=None,
    )
    database = Database(settings)
    database.create_schema()
    with database.session_factory() as session:
        course = Course(name="课程")
        batch = ImportBatch(course=course, name="批次", source_relative_path="课程/批次", status="question_review")
        images = []
        for index in range(2):
            source = settings.data_dir / f"imports/{index}.png"
            source.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (120, 200), "white").save(source)
            image = SourceImage(
                batch=batch,
                source_relative_path=f"{index}.png",
                original_name=f"{index}.png",
                safe_name=f"{index}.png",
                relative_path=f"imports/{index}.png",
                sha256=str(index + 1) * 64,
                mime_type="image/png",
                width=120,
                height=200,
                file_size=source.stat().st_size,
                order_index=index,
                ocr_status="completed",
            )
            images.append(image)
        session.add_all([batch, *images])
        session.flush()
        candidates = []
        for index, image in enumerate(images):
            crop = settings.data_dir / f"crops/{index}.png"
            crop.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (120, 100), "white").save(crop)
            candidate = QuestionCandidate(
                batch_id=batch.id,
                source_image=image,
                question_type="single_choice",
                stem=f"片段{index + 1}",
                options_json=[{"label": "A", "text": "甲", "order_index": 0}, {"label": "B", "text": "乙", "order_index": 1}],
                correct_answer_json="B" if index else None,
                raw_ocr_text=f"片段{index + 1}",
                normalized_ocr_text=f"片段{index + 1}",
                ocr_confidence=0.8,
                parse_confidence=0.7,
                display_mode="text",
                anomaly_flags_json=[{"code": "TRUNCATED_BOTTOM", "severity": "error", "message": "截断"}],
                status="review",
            )
            session.add(candidate)
            session.flush()
            session.add(CandidateImagePart(candidate_id=candidate.id, source_image_id=image.id, crop_path=f"crops/{index}.png", source_y_start=20, source_y_end=120, part_index=0))
            candidates.append(candidate)
        relationship = CandidateRelationship(
            batch_id=batch.id,
            primary_candidate_id=candidates[0].id,
            secondary_candidate_id=candidates[1].id,
            relation_type="cross_screenshot",
            decision="suggested",
            confidence=0.88,
            evidence_json={"merged_text_preview": "片段1\n片段2"},
            status="pending",
        )
        session.add(relationship)
        batch.found_question_count = 2
        batch.review_count = 2
        session.commit()
        return database, settings, batch.id, images[0].id, candidates[0].id, candidates[1].id, relationship.id


def _configure_repeated_title_pair(
    session,
    first_id: str,
    second_id: str,
    relationship_id: str,
    *,
    question_type: str = "single_choice",
) -> None:
    first = session.get(QuestionCandidate, first_id)
    second = session.get(QuestionCandidate, second_id)
    relationship = session.get(CandidateRelationship, relationship_id)
    assert first is not None and second is not None and relationship is not None
    relationship.evidence_json = {
        **relationship.evidence_json,
        "continuation_mode": "repeated_title",
    }
    first.source_display_number = "7"
    second.source_display_number = "7"
    first.question_type = question_type
    second.question_type = question_type
    first.display_mode = "text_first"
    first.explanation = "前屏解析"
    if question_type == "single_choice":
        first.stem = "示例命题"
        first.raw_ocr_text = "7.[单选题]\n示例命题\nA. 甲"
        first.normalized_ocr_text = first.raw_ocr_text
        first.options_json = [{"label": "A", "text": "甲", "order_index": 0}]
        first.correct_answer_json = "D"
        second.stem = "示例命题完整内容"
        second.raw_ocr_text = (
            "7.[单选题]\n示例命题完整内容\nA. 甲\nB. 乙\n"
            "C. 丙\nD. 丁\n正确答案:D"
        )
        second.normalized_ocr_text = second.raw_ocr_text
        second.options_json = []
        second.correct_answer_json = None
    else:
        first.stem = "示例判断命题"
        first.raw_ocr_text = "7.[判断题]\n示例判断命题"
        first.normalized_ocr_text = first.raw_ocr_text
        first.options_json = []
        first.correct_answer_json = False
        second.stem = "示例判断命题完整内容"
        second.raw_ocr_text = (
            "7.[判断题]\n示例判断命题完整内容\n正确答案:错误"
        )
        second.normalized_ocr_text = second.raw_ocr_text
        second.options_json = []
        second.correct_answer_json = None
    second.ocr_confidence = 0.98
    second.source_image.ocr_result_json = _repeated_title_page_result(
        question_type=question_type
    ).model_dump(mode="json")
    first.anomaly_flags_json = [
        {"code": "TRUNCATED_BOTTOM", "severity": "error", "message": "截断"}
    ]
    second.anomaly_flags_json = []
    session.commit()


def _relationship_action_count(session, relationship_id: str) -> int:
    return session.scalar(
        select(func.count(ReviewAction.id)).where(
            ReviewAction.target_type == "relationship",
            ReviewAction.target_id == relationship_id,
        )
    )


def _repeated_title_state(
    session,
    batch_id: str,
    first_id: str,
    second_id: str,
    relationship_id: str,
) -> dict:
    session.expire_all()
    first = session.get(QuestionCandidate, first_id)
    second = session.get(QuestionCandidate, second_id)
    relationship = session.get(CandidateRelationship, relationship_id)
    batch = session.get(ImportBatch, batch_id)
    assert first is not None and second is not None
    assert relationship is not None and batch is not None
    return {
        "first": (
            first.source_display_number,
            first.question_type,
            first.stem,
            first.options_json,
            first.correct_answer_json,
            first.raw_ocr_text,
            first.normalized_ocr_text,
            first.status,
            tuple((part.id, part.part_index) for part in first.image_parts),
        ),
        "second": (
            second.source_display_number,
            second.question_type,
            second.stem,
            second.options_json,
            second.correct_answer_json,
            second.status,
            tuple((part.id, part.part_index) for part in second.image_parts),
        ),
        "relationship": (relationship.status, relationship.decision),
        "batch": (batch.status, batch.review_count, batch.found_question_count),
        "active_actions": session.scalar(
            select(func.count(ReviewAction.id)).where(
                ReviewAction.target_type == "relationship",
                ReviewAction.target_id == relationship_id,
                ReviewAction.undone_at.is_(None),
            )
        ),
    }


def test_record_action_persists_one_microsecond_order_after_commit_reload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, _, _, _, first_id, _, _ = _seed(tmp_path)
    frozen = datetime(2099, 1, 1, tzinfo=timezone.utc)

    with database.session_factory() as session:
        candidate = session.get(QuestionCandidate, first_id)
        session.add(
            ReviewAction(
                batch_id=candidate.batch_id,
                candidate_id=candidate.id,
                target_type="candidate",
                target_id=candidate.id,
                action_type="update",
                before_json={},
                after_json={},
                created_at=frozen,
            )
        )
        session.commit()

    _freeze_review_action_clock(monkeypatch, frozen)
    with database.session_factory() as session:
        candidate = session.get(QuestionCandidate, first_id)
        action = snapshots.record_action(
            session,
            candidate,
            "update",
            {},
            {},
        )
        action_id = action.id
        session.commit()

    with database.session_factory() as session:
        persisted = session.get(ReviewAction, action_id)
        assert persisted.created_at == datetime(2099, 1, 1, 0, 0, 0, 1)


def test_record_action_uses_candidate_latest_plus_microsecond_when_clock_moves_backwards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, _, _, _, first_id, second_id, _ = _seed(tmp_path)
    latest = datetime(2099, 1, 1, tzinfo=timezone.utc)
    backwards_now = datetime(2098, 1, 1, tzinfo=timezone.utc)

    with database.session_factory() as session:
        candidate = session.get(QuestionCandidate, first_id)
        session.add(
            ReviewAction(
                batch_id=candidate.batch_id,
                candidate_id=candidate.id,
                target_type="candidate",
                target_id=candidate.id,
                action_type="update",
                before_json={},
                after_json={},
                undone_at=latest + timedelta(microseconds=10),
                created_at=latest,
            )
        )
        session.commit()

    _freeze_review_action_clock(monkeypatch, backwards_now)
    with database.session_factory() as session:
        first = session.get(QuestionCandidate, first_id)
        second = session.get(QuestionCandidate, second_id)
        first_action = snapshots.record_action(session, first, "update", {}, {})
        second_action = snapshots.record_action(session, second, "update", {}, {})

        assert first_action.created_at == latest + timedelta(microseconds=1)
        assert second_action.created_at == backwards_now


def test_undo_prefers_later_action_when_clock_ties_and_uuid_order_is_reversed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, settings, _, _, first_id, second_id, relationship_id = _seed(tmp_path)
    frozen = datetime(2099, 1, 1, tzinfo=timezone.utc)
    _freeze_review_action_clock(monkeypatch, frozen)

    with database.session_factory() as session:
        first = session.get(QuestionCandidate, first_id)
        second = session.get(QuestionCandidate, second_id)
        first.raw_ocr_text = "1.[单选题]\nOCR 原题干"
        first.normalized_ocr_text = first.raw_ocr_text
        second.raw_ocr_text = "A. OCR甲\nB. OCR乙\n正确答案:A"
        second.normalized_ocr_text = second.raw_ocr_text
        session.commit()

        update_candidate(
            session,
            settings,
            first_id,
            {
                "edited_ocr_text": "人工 OCR",
                "stem": "人工题干",
                "options": [
                    {"label": "A", "text": "人工甲", "order_index": 0},
                    {"label": "B", "text": "人工乙", "order_index": 1},
                ],
                "correct_answer": "B",
                "explanation": "人工解析",
            },
        )
        update_action = session.scalar(
            select(ReviewAction).where(
                ReviewAction.candidate_id == first_id,
                ReviewAction.action_type == "update",
            )
        )
        update_action.created_at = frozen
        update_action.id = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        session.flush()

        merge_candidates(session, first_id, second_id, relationship_id)

        merge_action = session.scalar(
            select(ReviewAction).where(
                ReviewAction.candidate_id == first_id,
                ReviewAction.action_type == "merge",
            )
        )
        merge_action.id = "00000000-0000-0000-0000-000000000000"
        session.flush()

        restored = undo_last_action(session, settings, first_id)

        assert restored.stem == "人工题干"
        assert restored.correct_answer_json == "B"
        assert restored.edited_ocr_text == "人工 OCR"
        assert len(restored.image_parts) == 1


def _resolution_state(
    session,
    batch_id: str,
    first_id: str,
    second_id: str,
    relationship_id: str,
) -> dict:
    session.expire_all()
    relationship = session.get(CandidateRelationship, relationship_id)
    first = session.get(QuestionCandidate, first_id)
    second = session.get(QuestionCandidate, second_id)
    batch = session.get(ImportBatch, batch_id)
    return {
        "relationship": (
            relationship.status,
            relationship.decision,
            relationship.primary_candidate_id,
            relationship.secondary_candidate_id,
        ),
        "candidates": (first.status, second.status),
        "batch": (batch.status, batch.review_count),
        "parts": session.scalar(select(func.count(CandidateImagePart.id))),
        "actions": _relationship_action_count(session, relationship_id),
    }


def _run_concurrent_manual_merges(
    database: Database,
    orientations: list[tuple[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, str | None]]:
    lookup_barrier = Barrier(2)
    original_scalar = Session.scalar

    def synchronized_scalar(session, statement, *args, **kwargs):
        descriptions = getattr(statement, "column_descriptions", ())
        selects_relationship = any(
            description.get("entity") is CandidateRelationship
            for description in descriptions
        )
        if (
            session.info.get("synchronize_manual_relationship_lookup")
            and not session.info.get("manual_relationship_lookup_synchronized")
            and selects_relationship
        ):
            session.info["manual_relationship_lookup_synchronized"] = True
            try:
                lookup_barrier.wait(timeout=0.5)
            except BrokenBarrierError:
                pass
        return original_scalar(session, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "scalar", synchronized_scalar)

    def merge(orientation: tuple[str, str]) -> tuple[str, str | None]:
        with database.session_factory() as session:
            session.info["synchronize_manual_relationship_lookup"] = True
            try:
                merged = merge_candidates(session, *orientation)
            except ReviewRelationshipConflictError:
                return "conflict", None
            except Exception as error:  # pragma: no cover - asserted as a failure result
                return "error", type(error).__name__
            return "success", merged.id

    with ThreadPoolExecutor(max_workers=2) as pool:
        return list(pool.map(merge, orientations))


def test_merge_preserves_original_fragments_and_undo_restores_relationship(tmp_path: Path) -> None:
    database, settings, _, _, first_id, second_id, relationship_id = _seed(tmp_path)
    with database.session_factory() as session:
        merged = merge_candidates(session, first_id, second_id, relationship_id)
        second = session.get(QuestionCandidate, second_id)
        relationship = session.get(CandidateRelationship, relationship_id)
        assert len(merged.image_parts) == 2
        assert second.status == "merged_source" and len(second.image_parts) == 1
        assert relationship.status == "resolved" and relationship.decision == "confirmed"

        restored = undo_last_action(session, settings, first_id)
        second = session.get(QuestionCandidate, second_id)
        relationship = session.get(CandidateRelationship, relationship_id)
        assert len(restored.image_parts) == 1
        assert second.status == "review"
        assert relationship.status == "pending" and relationship.decision == "suggested"


def test_repeating_confirmed_merge_is_idempotent(tmp_path: Path) -> None:
    database, _, _, _, first_id, second_id, relationship_id = _seed(tmp_path)

    with database.session_factory() as session:
        first = merge_candidates(session, first_id, second_id, relationship_id)
        part_ids = [part.id for part in first.image_parts]
        action_count = session.scalar(
            select(func.count(ReviewAction.id)).where(
                ReviewAction.candidate_id == first_id,
                ReviewAction.action_type == "merge",
            )
        )

        repeated = merge_candidates(session, first_id, second_id, relationship_id)

        assert [part.id for part in repeated.image_parts] == part_ids
        assert session.scalar(
            select(func.count(ReviewAction.id)).where(
                ReviewAction.candidate_id == first_id,
                ReviewAction.action_type == "merge",
            )
        ) == action_count


@pytest.mark.parametrize("retry_entrypoint", ["merge", "resolve"])
def test_exact_confirmed_merge_retry_releases_sqlite_writer(
    tmp_path: Path,
    retry_entrypoint: str,
) -> None:
    database, _, _, _, first_id, second_id, relationship_id = _seed(tmp_path)

    with database.session_factory() as session:
        merge_candidates(session, first_id, second_id, relationship_id)
        if retry_entrypoint == "merge":
            repeated_id = merge_candidates(
                session,
                first_id,
                second_id,
                relationship_id,
            ).id
            assert repeated_id == first_id
        else:
            repeated_id = resolve_relationship(
                session,
                relationship_id,
                "confirm",
            ).id
            assert repeated_id == relationship_id

        with database.session_factory() as writer:
            writer.execute(text("PRAGMA busy_timeout=100"))
            writer.add(Course(name=f"writer-after-{retry_entrypoint}"))
            try:
                writer.commit()
            except OperationalError:
                writer.rollback()
                writer_committed = False
            else:
                writer_committed = True

        assert writer_committed


@pytest.mark.parametrize("repeated_decision", ["only_primary", "only_secondary"])
def test_processed_relationship_rejects_replay_without_side_effects(
    tmp_path: Path,
    repeated_decision: str,
) -> None:
    database, _, batch_id, _, first_id, second_id, relationship_id = _seed(tmp_path)
    with database.session_factory() as session:
        resolve_relationship(session, relationship_id, "only_primary")
        expected = _resolution_state(
            session, batch_id, first_id, second_id, relationship_id
        )

        with pytest.raises(ReviewRelationshipConflictError):
            resolve_relationship(session, relationship_id, repeated_decision)

        assert _resolution_state(
            session, batch_id, first_id, second_id, relationship_id
        ) == expected
        assert expected["relationship"][:2] == ("resolved", "kept_primary")
        assert expected["candidates"] == ("review", "ignored")


def test_direct_merge_cannot_override_resolved_non_merge_relationship(
    tmp_path: Path,
) -> None:
    database, _, batch_id, _, first_id, second_id, relationship_id = _seed(tmp_path)
    with database.session_factory() as session:
        resolve_relationship(session, relationship_id, "separate")
        expected = _resolution_state(
            session, batch_id, first_id, second_id, relationship_id
        )

        with pytest.raises(ReviewRelationshipConflictError):
            merge_candidates(session, first_id, second_id, relationship_id)

        assert _resolution_state(
            session, batch_id, first_id, second_id, relationship_id
        ) == expected


def test_concurrent_terminal_relationship_decisions_have_one_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, _, batch_id, _, first_id, second_id, relationship_id = _seed(tmp_path)
    barrier = Barrier(2)
    original_claim = actions.claim_pending_relationship

    def synchronized_claim(session, claimed_relationship_id: str):
        barrier.wait(timeout=5)
        return original_claim(session, claimed_relationship_id)

    monkeypatch.setattr(
        actions,
        "claim_pending_relationship",
        synchronized_claim,
    )

    def decide(decision: str) -> tuple[str, str]:
        with database.session_factory() as session:
            try:
                relationship = resolve_relationship(
                    session, relationship_id, decision
                )
            except ReviewRelationshipConflictError:
                return "conflict", decision
            return "success", relationship.decision

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(decide, ["only_primary", "only_secondary"])
        )

    assert sorted(result[0] for result in results) == ["conflict", "success"]
    winning_decision = next(
        result[1] for result in results if result[0] == "success"
    )
    expected_candidates = {
        "kept_primary": ("review", "ignored"),
        "kept_secondary": ("ignored", "review"),
    }[winning_decision]
    with database.session_factory() as session:
        state = _resolution_state(
            session,
            batch_id,
            first_id,
            second_id,
            relationship_id,
        )
        assert state["relationship"] == (
            "resolved",
            winning_decision,
            first_id,
            second_id,
        )
        assert state["candidates"] == expected_candidates
        assert state["batch"] == ("question_review", 1)
        assert state["parts"] == 2
        assert state["actions"] == 1


def test_concurrent_same_orientation_manual_merges_share_idempotent_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, _, batch_id, _, first_id, second_id, relationship_id = _seed(tmp_path)
    with database.session_factory() as session:
        session.delete(session.get(CandidateRelationship, relationship_id))
        session.commit()

    results = _run_concurrent_manual_merges(
        database,
        [(first_id, second_id), (first_id, second_id)],
        monkeypatch,
    )

    assert results == [("success", first_id), ("success", first_id)]
    with database.session_factory() as session:
        relationships = list(session.scalars(select(CandidateRelationship)))
        assert len(relationships) == 1
        relationship = relationships[0]
        assert (
            relationship.status,
            relationship.decision,
            relationship.primary_candidate_id,
            relationship.secondary_candidate_id,
        ) == ("resolved", "confirmed", first_id, second_id)
        assert session.get(QuestionCandidate, first_id).status == "review"
        assert session.get(QuestionCandidate, second_id).status == "merged_source"
        assert session.scalar(
            select(func.count(CandidateImagePart.id)).where(
                CandidateImagePart.candidate_id == first_id
            )
        ) == 2
        assert session.scalar(
            select(func.count(CandidateImagePart.id)).where(
                CandidateImagePart.candidate_id == second_id
            )
        ) == 1
        batch = session.get(ImportBatch, batch_id)
        assert (batch.status, batch.found_question_count, batch.review_count) == (
            "question_review",
            1,
            1,
        )
        assert _relationship_action_count(session, relationship.id) == 1


def test_concurrent_reversed_manual_merges_match_the_winning_orientation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, _, batch_id, _, first_id, second_id, relationship_id = _seed(tmp_path)
    with database.session_factory() as session:
        session.delete(session.get(CandidateRelationship, relationship_id))
        session.commit()
    orientations = [(first_id, second_id), (second_id, first_id)]

    results = _run_concurrent_manual_merges(database, orientations, monkeypatch)

    assert sorted(result[0] for result in results) == ["conflict", "success"]
    winner_index = next(
        index for index, result in enumerate(results) if result[0] == "success"
    )
    winning_primary, winning_secondary = orientations[winner_index]
    assert results[winner_index] == ("success", winning_primary)
    with database.session_factory() as session:
        relationships = list(session.scalars(select(CandidateRelationship)))
        assert len(relationships) == 1
        relationship = relationships[0]
        assert (
            relationship.status,
            relationship.decision,
            relationship.primary_candidate_id,
            relationship.secondary_candidate_id,
        ) == (
            "resolved",
            "confirmed",
            winning_primary,
            winning_secondary,
        )
        assert session.get(QuestionCandidate, winning_primary).status == "review"
        assert (
            session.get(QuestionCandidate, winning_secondary).status
            == "merged_source"
        )
        assert session.scalar(
            select(func.count(CandidateImagePart.id)).where(
                CandidateImagePart.candidate_id == winning_primary
            )
        ) == 2
        assert session.scalar(
            select(func.count(CandidateImagePart.id)).where(
                CandidateImagePart.candidate_id == winning_secondary
            )
        ) == 1
        batch = session.get(ImportBatch, batch_id)
        assert (batch.status, batch.found_question_count, batch.review_count) == (
            "question_review",
            1,
            1,
        )
        assert _relationship_action_count(session, relationship.id) == 1


def test_swap_keeps_relationship_open_for_one_terminal_decision(tmp_path: Path) -> None:
    database, _, _, _, first_id, second_id, relationship_id = _seed(tmp_path)
    with database.session_factory() as session:
        swapped = resolve_relationship(session, relationship_id, "swap")
        assert swapped.status == "pending"
        assert swapped.primary_candidate_id == second_id
        assert swapped.secondary_candidate_id == first_id

        resolved = resolve_relationship(session, relationship_id, "only_primary")
        assert resolved.status == "resolved"
        assert resolved.decision == "kept_primary"
        assert session.get(QuestionCandidate, first_id).status == "ignored"


def test_repeated_title_confirm_promotes_fresh_complete_secondary_parse(
    tmp_path: Path,
) -> None:
    database, _, _, _, first_id, second_id, relationship_id = _seed(tmp_path)
    with database.session_factory() as session:
        _configure_repeated_title_pair(session, first_id, second_id, relationship_id)
        merged = merge_candidates(session, first_id, second_id, relationship_id)

        assert merged.source_display_number == "7"
        assert merged.question_type == "single_choice"
        assert merged.stem == "示例命题完整内容"
        assert [item["label"] for item in merged.options_json] == ["A", "B", "C", "D"]
        assert [item["text"] for item in merged.options_json] == ["甲", "乙", "丙", "丁"]
        assert merged.correct_answer_json == "D"
        assert merged.display_mode == "text_first"
        assert merged.explanation == "前屏解析"
        assert merged.normalized_ocr_text == (
            "7.[单选题]\n示例命题完整内容\nA. 甲\nB. 乙\n"
            "C. 丙\nD. 丁\n正确答案:D"
        )
        assert merged.raw_ocr_text.startswith("7.[单选题]\n示例命题\nA. 甲\n")
        assert merged.raw_ocr_text.endswith("D. 丁\n正确答案:D")
        assert merged.status == "review"
        assert len(merged.image_parts) == 2
        secondary = session.get(QuestionCandidate, second_id)
        relationship = session.get(CandidateRelationship, relationship_id)
        assert secondary is not None and secondary.status == "merged_source"
        assert relationship is not None
        assert (relationship.status, relationship.decision) == ("resolved", "confirmed")


def test_repeated_title_confirm_promotes_optionless_true_false_secondary(
    tmp_path: Path,
) -> None:
    database, _, _, _, first_id, second_id, relationship_id = _seed(tmp_path)
    with database.session_factory() as session:
        _configure_repeated_title_pair(
            session,
            first_id,
            second_id,
            relationship_id,
            question_type="true_false",
        )
        merged = merge_candidates(session, first_id, second_id, relationship_id)

        assert merged.stem == "示例判断命题完整内容"
        assert [(item["label"], item["text"]) for item in merged.options_json] == [
            ("A", "正确"),
            ("B", "错误"),
        ]
        assert merged.correct_answer_json is False
        assert (merged.true_label, merged.false_label) == ("A", "B")


@pytest.mark.parametrize(
    ("question_type", "expected_stem", "expected_answer"),
    [
        ("single_choice", "示例命题完整内容", "D"),
        ("true_false", "示例判断命题完整内容", False),
    ],
)
def test_repeated_title_confirm_preserves_spatial_widget_filter(
    tmp_path: Path,
    question_type: str,
    expected_stem: str,
    expected_answer: str | bool,
) -> None:
    database, _, _, _, first_id, second_id, relationship_id = _seed(tmp_path)
    with database.session_factory() as session:
        _configure_repeated_title_pair(
            session,
            first_id,
            second_id,
            relationship_id,
            question_type=question_type,
        )
        second = session.get(QuestionCandidate, second_id)
        assert second is not None
        if question_type == "single_choice":
            raw_text = (
                "7.[单选题]\n示例命题完整内容\nG\n写笔记\nA. 甲\nB. 乙\n"
                "C. 丙\nD. 丁\n正确答案:D"
            )
        else:
            raw_text = (
                "7.[判断题]\n示例判断命题完整内容\nG\n写笔记\n正确答案:错误"
            )
        second.raw_ocr_text = raw_text
        second.normalized_ocr_text = raw_text
        second.source_image.ocr_result_json = _repeated_title_page_result(
            question_type=question_type,
            include_widget=True,
        ).model_dump(mode="json")
        session.commit()

        merged = merge_candidates(session, first_id, second_id, relationship_id)

        assert merged.stem == expected_stem
        assert merged.correct_answer_json == expected_answer
        assert "G" not in merged.stem
        assert "写笔记" not in merged.stem
        assert all("写笔记" not in item["text"] for item in merged.options_json)
        assert "G\n写笔记" in merged.raw_ocr_text
        if question_type == "single_choice":
            assert [item["text"] for item in merged.options_json] == [
                "甲",
                "乙",
                "丙",
                "丁",
            ]
        else:
            assert [
                (item["label"], item["text"]) for item in merged.options_json
            ] == [("A", "正确"), ("B", "错误")]
            assert (merged.true_label, merged.false_label) == ("A", "B")


@pytest.mark.parametrize(
    "case",
    [
        "missing_payload",
        "invalid_payload",
        "zero_identity_match",
        "multiple_identity_matches",
        "raw_segment_mismatch",
        "confidence_mismatch",
    ],
)
def test_repeated_title_confirm_rolls_back_without_one_consistent_spatial_match(
    tmp_path: Path,
    case: str,
) -> None:
    database, _, batch_id, _, first_id, second_id, relationship_id = _seed(tmp_path)
    with database.session_factory() as session:
        _configure_repeated_title_pair(session, first_id, second_id, relationship_id)
        second = session.get(QuestionCandidate, second_id)
        assert second is not None
        if case == "missing_payload":
            second.source_image.ocr_result_json = None
        elif case == "invalid_payload":
            second.source_image.ocr_result_json = {"page_width": 0}
        elif case == "zero_identity_match":
            second.source_image.ocr_result_json = _repeated_title_page_result(
                display_number="9"
            ).model_dump(mode="json")
        elif case == "multiple_identity_matches":
            second.source_image.ocr_result_json = _repeated_title_page_result(
                duplicate_match=True
            ).model_dump(mode="json")
        elif case == "raw_segment_mismatch":
            second.source_image.ocr_result_json = _repeated_title_page_result(
                stem="页面中的另一段题干"
            ).model_dump(mode="json")
        else:
            second.source_image.ocr_result_json = _repeated_title_page_result(
                confidence=0.97
            ).model_dump(mode="json")
        session.commit()
        before = _repeated_title_state(
            session, batch_id, first_id, second_id, relationship_id
        )

        with pytest.raises(ReviewValidationError):
            merge_candidates(session, first_id, second_id, relationship_id)

        assert _repeated_title_state(
            session, batch_id, first_id, second_id, relationship_id
        ) == before


@pytest.mark.parametrize(
    "case",
    [
        "empty_identity",
        "different_identity",
        "different_type",
        "multiple_secondary_questions",
        "secondary_unsupported",
        "secondary_anomaly",
        "secondary_missing_answer",
        "secondary_too_few_options",
        "primary_stem_not_covered",
    ],
)
def test_repeated_title_confirm_fails_closed_when_safety_gate_is_missing(
    tmp_path: Path,
    case: str,
) -> None:
    database, _, batch_id, _, first_id, second_id, relationship_id = _seed(tmp_path)
    with database.session_factory() as session:
        _configure_repeated_title_pair(session, first_id, second_id, relationship_id)
        first = session.get(QuestionCandidate, first_id)
        second = session.get(QuestionCandidate, second_id)
        assert first is not None and second is not None
        if case == "empty_identity":
            second.source_display_number = ""
        elif case == "different_identity":
            second.source_display_number = "8"
        elif case == "different_type":
            second.question_type = "multiple_choice"
        elif case == "multiple_secondary_questions":
            second.raw_ocr_text += "\n8.[单选题]\n另一题\nA.甲\nB.乙\n正确答案:A"
        elif case == "secondary_unsupported":
            second.raw_ocr_text = "无法识别为题目的文本"
        elif case == "secondary_anomaly":
            second.ocr_confidence = 0.1
        elif case == "secondary_missing_answer":
            second.raw_ocr_text = "7.[单选题]\n示例命题完整内容\nA.甲\nB.乙"
        elif case == "secondary_too_few_options":
            second.raw_ocr_text = "7.[单选题]\n示例命题完整内容\nA.甲\n正确答案:A"
        else:
            first.stem = "前屏独有且后屏没有的题干"
        session.commit()
        before = _repeated_title_state(
            session, batch_id, first_id, second_id, relationship_id
        )

        with pytest.raises(ReviewValidationError):
            merge_candidates(session, first_id, second_id, relationship_id)

        assert _repeated_title_state(
            session, batch_id, first_id, second_id, relationship_id
        ) == before


def test_repeated_title_confirm_preserves_prior_manual_primary_fields(
    tmp_path: Path,
) -> None:
    database, settings, _, _, first_id, second_id, relationship_id = _seed(tmp_path)
    with database.session_factory() as session:
        _configure_repeated_title_pair(session, first_id, second_id, relationship_id)
        update_candidate(
            session,
            settings,
            first_id,
            {
                "edited_ocr_text": "人工 OCR",
                "stem": "人工题干",
                "options": [
                    {"label": "A", "text": "人工甲", "order_index": 0},
                    {"label": "B", "text": "人工乙", "order_index": 1},
                ],
                "correct_answer": "B",
            },
        )
        merged = merge_candidates(session, first_id, second_id, relationship_id)
        assert (merged.stem, merged.correct_answer_json, merged.edited_ocr_text) == (
            "人工题干",
            "B",
            "人工 OCR",
        )
        assert [item["text"] for item in merged.options_json] == ["人工甲", "人工乙"]


def test_repeated_title_confirm_survives_reload_and_undo_restores_all_state(
    tmp_path: Path,
) -> None:
    database, settings, batch_id, _, first_id, second_id, relationship_id = _seed(
        tmp_path
    )
    with database.session_factory() as session:
        _configure_repeated_title_pair(session, first_id, second_id, relationship_id)
        before = _repeated_title_state(
            session, batch_id, first_id, second_id, relationship_id
        )
        merge_candidates(session, first_id, second_id, relationship_id)
    with database.session_factory() as session:
        reloaded = session.get(QuestionCandidate, first_id)
        assert reloaded is not None
        assert [item["label"] for item in reloaded.options_json] == ["A", "B", "C", "D"]
        undo_last_action(session, settings, first_id)
        assert _repeated_title_state(
            session, batch_id, first_id, second_id, relationship_id
        ) == before


def test_repeated_title_confirm_retry_is_idempotent_after_reload(
    tmp_path: Path,
) -> None:
    database, _, _, _, first_id, second_id, relationship_id = _seed(tmp_path)
    with database.session_factory() as session:
        _configure_repeated_title_pair(session, first_id, second_id, relationship_id)
        merge_candidates(session, first_id, second_id, relationship_id)
    with database.session_factory() as session:
        repeated = merge_candidates(session, first_id, second_id, relationship_id)
        assert [item["label"] for item in repeated.options_json] == ["A", "B", "C", "D"]
        assert _relationship_action_count(session, relationship_id) == 1
        assert len(repeated.image_parts) == 2


def test_concurrent_repeated_title_confirm_and_separate_have_one_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, _, _, _, first_id, second_id, relationship_id = _seed(tmp_path)
    with database.session_factory() as session:
        _configure_repeated_title_pair(session, first_id, second_id, relationship_id)
    barrier = Barrier(2)
    original_claim = actions.claim_pending_relationship

    def synchronized_claim(session, claimed_relationship_id: str):
        barrier.wait(timeout=5)
        return original_claim(session, claimed_relationship_id)

    monkeypatch.setattr(actions, "claim_pending_relationship", synchronized_claim)

    def decide(decision: str) -> tuple[str, str]:
        with database.session_factory() as session:
            try:
                relationship = resolve_relationship(session, relationship_id, decision)
            except ReviewRelationshipConflictError:
                return "conflict", decision
            return "success", relationship.decision

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(decide, ["confirm", "separate"]))

    assert sorted(item[0] for item in results) == ["conflict", "success"]
    winner = next(item[1] for item in results if item[0] == "success")
    with database.session_factory() as session:
        first = session.get(QuestionCandidate, first_id)
        second = session.get(QuestionCandidate, second_id)
        relationship = session.get(CandidateRelationship, relationship_id)
        assert first is not None and second is not None and relationship is not None
        assert relationship.status == "resolved"
        assert _relationship_action_count(session, relationship_id) == 1
        if winner == "confirmed":
            assert [item["label"] for item in first.options_json] == [
                "A",
                "B",
                "C",
                "D",
            ]
            assert (first.status, second.status) == ("review", "merged_source")
            assert len(first.image_parts) == 2
        else:
            assert winner == "kept_separate"
            assert [item["label"] for item in first.options_json] == ["A"]
            assert (first.status, second.status) == ("review", "review")
            assert len(first.image_parts) == 1


def test_cross_screenshot_merge_reparses_combined_options_and_answer(tmp_path: Path) -> None:
    database, settings, _, _, first_id, second_id, relationship_id = _seed(tmp_path)
    with database.session_factory() as session:
        first = session.get(QuestionCandidate, first_id)
        second = session.get(QuestionCandidate, second_id)
        first.stem = "跨截图合成题的前半部分"
        first.raw_ocr_text = "1.[单选题]\n跨截图合成题的前半部分\n题干继续"
        first.normalized_ocr_text = first.raw_ocr_text
        first.options_json = []
        first.correct_answer_json = None
        second.stem = "题干继续\nA. 甲\nB. 乙\n正确答案:A"
        second.raw_ocr_text = second.stem
        second.normalized_ocr_text = second.raw_ocr_text
        second.options_json = []
        second.correct_answer_json = None
        session.commit()

        merged = merge_candidates(
            session,
            first_id,
            second_id,
            relationship_id,
            low_confidence_threshold=0.99,
        )

        assert merged.status == "review"
        assert "LOW_OCR_CONFIDENCE" in {
            flag["code"] for flag in merged.anomaly_flags_json
        }
        assert merged.source_display_number == "1"
        assert [option["label"] for option in merged.options_json] == ["A", "B"]
        assert [option["text"] for option in merged.options_json] == ["甲", "乙"]
        assert merged.correct_answer_json == "A"
        assert merged.display_mode == "text"
        assert len(merged.image_parts) == 2

        restored = undo_last_action(session, settings, first_id)
        assert restored.source_display_number is None
        assert restored.options_json == []
        assert restored.correct_answer_json is None
        assert len(restored.image_parts) == 1


def test_cross_screenshot_merge_preserves_prior_manual_fields_on_answer_conflict(tmp_path: Path) -> None:
    database, settings, _, _, first_id, second_id, relationship_id = _seed(tmp_path)
    with database.session_factory() as session:
        first = session.get(QuestionCandidate, first_id)
        second = session.get(QuestionCandidate, second_id)
        first.raw_ocr_text = "1.[单选题]\nOCR 原题干"
        first.normalized_ocr_text = first.raw_ocr_text
        second.raw_ocr_text = "A. OCR甲\nB. OCR乙\n正确答案:A"
        second.normalized_ocr_text = second.raw_ocr_text
        session.commit()
        update_candidate(
            session,
            settings,
            first_id,
            {
                "edited_ocr_text": "人工 OCR",
                "stem": "人工题干",
                "options": [
                    {"label": "A", "text": "人工甲", "order_index": 0},
                    {"label": "B", "text": "人工乙", "order_index": 1},
                ],
                "correct_answer": "B",
                "explanation": "人工解析",
            },
        )

        merged = merge_candidates(session, first_id, second_id, relationship_id)

        assert merged.stem == "人工题干"
        assert [option["text"] for option in merged.options_json] == ["人工甲", "人工乙"]
        assert merged.correct_answer_json == "B"
        assert merged.explanation == "人工解析"
        assert merged.edited_ocr_text == "人工 OCR"
        assert len(merged.image_parts) == 2

        restored = undo_last_action(session, settings, first_id)
        assert restored.stem == "人工题干"
        assert restored.correct_answer_json == "B"
        assert restored.edited_ocr_text == "人工 OCR"
        assert len(restored.image_parts) == 1


def test_cross_screenshot_merge_does_not_replace_existing_answer_when_only_options_missing(tmp_path: Path) -> None:
    database, _, _, _, first_id, second_id, relationship_id = _seed(tmp_path)
    with database.session_factory() as session:
        first = session.get(QuestionCandidate, first_id)
        second = session.get(QuestionCandidate, second_id)
        first.raw_ocr_text = "1.[单选题]\n保留已有答案"
        first.normalized_ocr_text = first.raw_ocr_text
        first.options_json = []
        first.correct_answer_json = "B"
        second.raw_ocr_text = "A. OCR甲\nB. OCR乙\n正确答案:A"
        second.normalized_ocr_text = second.raw_ocr_text
        session.commit()

        merged = merge_candidates(session, first_id, second_id, relationship_id)

        assert [option["label"] for option in merged.options_json] == ["A", "B"]
        assert merged.correct_answer_json == "B"


def test_editing_merged_crop_does_not_overwrite_original_secondary_fragment(tmp_path: Path) -> None:
    database, settings, _, _, first_id, second_id, relationship_id = _seed(tmp_path)
    with database.session_factory() as session:
        merged = merge_candidates(session, first_id, second_id, relationship_id)
        secondary = session.get(QuestionCandidate, second_id)
        original_path = secondary.image_parts[0].crop_path
        merged_part = merged.image_parts[1]
        update_candidate(
            session,
            settings,
            first_id,
            {
                "correct_answer": "B",
                "display_mode": "text_first",
                "parts": [{"id": merged_part.id, "part_index": 1, "source_y_start": 30, "source_y_end": 140}],
            },
        )
        primary = session.get(QuestionCandidate, first_id)
        secondary = session.get(QuestionCandidate, second_id)

    assert primary.image_parts[1].crop_path != original_path
    assert secondary.image_parts[0].crop_path == original_path
    assert secondary.image_parts[0].source_y_start == 20


def test_split_staging_failure_preserves_database_and_primary_crop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, settings, _, _, first_id, _, _ = _seed(tmp_path)
    primary_crop = settings.data_dir / "crops/0.png"
    original_bytes = primary_crop.read_bytes()
    real_crop = crop_files.crop_question_image
    calls = 0

    def fail_second_crop(source, target, bounds) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic second split crop failure")
        real_crop(source, target, bounds)

    monkeypatch.setattr(actions, "crop_question_image", fail_second_crop)
    monkeypatch.setattr(crop_files, "crop_question_image", fail_second_crop)

    with database.session_factory() as session:
        before_count = session.scalar(select(func.count(QuestionCandidate.id)))
        with pytest.raises(OSError, match="synthetic second split crop failure"):
            split_candidate(
                session,
                settings,
                first_id,
                split_y=70,
                second_stem="后半题",
            )

    with database.session_factory() as session:
        first = session.get(QuestionCandidate, first_id)
        assert first.image_parts[0].source_y_end == 120
        assert (
            session.scalar(select(func.count(QuestionCandidate.id)))
            == before_count
        )
    assert primary_crop.read_bytes() == original_bytes
    assert not list(primary_crop.parent.glob(".*.recrop-*"))


def test_split_install_failure_restores_files_and_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, settings, _, _, first_id, _, _ = _seed(tmp_path)
    primary_crop = settings.data_dir / "crops/0.png"
    original_bytes = primary_crop.read_bytes()
    original_pngs = {
        path.relative_to(settings.data_dir)
        for path in settings.data_dir.rglob("*.png")
    }
    real_replace = os.replace
    staged_install_count = 0

    def fail_second_staged_install(source, target) -> None:
        nonlocal staged_install_count
        if ".recrop-stage-" in Path(source).name:
            staged_install_count += 1
            if staged_install_count == 2:
                raise OSError("synthetic second split install failure")
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", fail_second_staged_install)

    with database.session_factory() as session:
        before_count = session.scalar(select(func.count(QuestionCandidate.id)))
        with pytest.raises(OSError, match="synthetic second split install failure"):
            split_candidate(
                session,
                settings,
                first_id,
                split_y=70,
                second_stem="后半题",
            )

    with database.session_factory() as session:
        first = session.get(QuestionCandidate, first_id)
        assert first.image_parts[0].source_y_end == 120
        assert (
            session.scalar(select(func.count(QuestionCandidate.id)))
            == before_count
        )
    assert primary_crop.read_bytes() == original_bytes
    assert {
        path.relative_to(settings.data_dir)
        for path in settings.data_dir.rglob("*.png")
    } == original_pngs
    assert not list((settings.data_dir / "crops").rglob(".*.recrop-*"))


def test_split_order_duplicate_and_mock_ocr_actions(tmp_path: Path) -> None:
    database, settings, _, first_image_id, first_id, second_id, _ = _seed(tmp_path)
    engine = MockOcrEngine(default=_result())
    with database.session_factory() as session:
        second = split_candidate(session, settings, first_id, split_y=70, second_stem="后半题")
        first = session.get(QuestionCandidate, first_id)
        assert first.image_parts[0].source_y_end == 70
        assert second.image_parts[0].source_y_start == 70

        move_source_image(session, first_id, "down")
        order = list(session.scalars(select(SourceImage).order_by(SourceImage.order_index)))
        assert order[-1].id == first_image_id

        duplicate = mark_duplicate(session, second_id, first_id)
        assert duplicate.status == "duplicate_merged" and duplicate.duplicate_of_id == first_id

        recognized = rerun_candidate_ocr(session, settings, engine, first_id)
        assert recognized.stem == "重新识别题干"
        assert recognized.correct_answer_json == "B"
        assert engine.calls


@pytest.mark.parametrize(
    "operation",
    [confirm_candidate, ignore_candidate, delete_candidate],
    ids=["confirm", "ignore", "delete"],
)
def test_pending_relationship_blocks_direct_candidate_state_changes(
    tmp_path: Path,
    operation,
) -> None:
    database, _, _, _, _, second_id, relationship_id = _seed(tmp_path)

    with database.session_factory() as session:
        with pytest.raises(ReviewValidationError, match="先处理.*候选关系"):
            operation(session, second_id)
        session.rollback()

    with database.session_factory() as session:
        assert session.get(QuestionCandidate, second_id).status == "review"
        assert session.get(CandidateRelationship, relationship_id).status == "pending"


def test_batch_state_demotes_ready_candidate_with_pending_relationship(tmp_path: Path) -> None:
    database, _, batch_id, _, _, second_id, _ = _seed(tmp_path)

    with database.session_factory() as session:
        session.get(QuestionCandidate, second_id).status = "ready"
        session.commit()

        batch = update_batch_review_state(session, batch_id)

        assert session.get(QuestionCandidate, second_id).status == "review"
        assert batch.status == "question_review"
        assert batch.high_confidence_count == 0
        assert batch.review_count == 2


def test_merged_source_is_excluded_from_batch_question_count(tmp_path: Path) -> None:
    database, _, batch_id, _, first_id, second_id, _ = _seed(tmp_path)

    with database.session_factory() as session:
        merge_candidates(session, first_id, second_id)

        batch = session.get(ImportBatch, batch_id)
        assert session.get(QuestionCandidate, second_id).status == "merged_source"
        assert batch.found_question_count == 1
        assert batch.review_count == 1


def test_candidate_ocr_rerun_stays_in_review_while_relationship_is_pending(tmp_path: Path) -> None:
    database, settings, _, _, _, second_id, relationship_id = _seed(tmp_path)
    engine = MockOcrEngine(default=_result())

    with database.session_factory() as session:
        recognized = rerun_candidate_ocr(session, settings, engine, second_id)

        assert recognized.correct_answer_json == "B"
        assert recognized.status == "review"
        assert session.get(CandidateRelationship, relationship_id).status == "pending"


def test_worker_can_rerun_one_source_image_with_mock_ocr(tmp_path: Path) -> None:
    database, settings, _, first_image_id, first_id, _, _ = _seed(tmp_path)
    engine = MockOcrEngine(default=_result("整图重新识别", page_height=200))
    worker = PersistentOcrWorker(database, settings, engine)

    worker.reprocess_image(first_image_id)

    with database.session_factory() as session:
        image = session.get(SourceImage, first_image_id)
        candidate = session.get(QuestionCandidate, first_id)
        assert "整图重新识别" in image.ocr_text
        assert candidate.stem == "整图重新识别"
        assert candidate.correct_answer_json == "B"
        restored = undo_last_action(session, settings, first_id)
        assert restored.stem == "片段1"


def test_relationship_and_duplicate_decisions_are_fully_undoable(tmp_path: Path) -> None:
    database, settings, _, _, first_id, second_id, relationship_id = _seed(tmp_path)
    with database.session_factory() as session:
        resolved = resolve_relationship(session, relationship_id, "only_primary")
        assert resolved.status == "resolved"
        assert session.get(QuestionCandidate, second_id).status == "ignored"
        undo_last_action(session, settings, first_id)
        assert session.get(QuestionCandidate, second_id).status == "review"
        assert session.get(CandidateRelationship, relationship_id).status == "pending"

        mark_duplicate(session, second_id, first_id)
        manual_id = session.scalar(
            select(CandidateRelationship.id).where(CandidateRelationship.relation_type == "manual_duplicate")
        )
        undo_last_action(session, settings, second_id)
        assert session.get(QuestionCandidate, second_id).status == "review"
        assert session.get(QuestionCandidate, second_id).duplicate_of_id is None
        assert session.get(CandidateRelationship, manual_id) is None


def test_duplicate_review_reuses_pending_relationship_and_undo_restores_it(tmp_path: Path) -> None:
    database, settings, _, _, first_id, second_id, relationship_id = _seed(tmp_path)
    with database.session_factory() as session:
        relationship = session.get(CandidateRelationship, relationship_id)
        relationship.relation_type = "same_batch_duplicate"
        relationship.decision = "review"
        relationship.status = "pending"
        session.commit()

        marked = mark_duplicate(session, second_id, first_id)
        relationships = list(session.scalars(select(CandidateRelationship)))
        assert marked.status == "duplicate_merged"
        assert [item.id for item in relationships] == [relationship_id]
        assert relationships[0].decision == "confirmed"
        assert relationships[0].status == "resolved"

        undo_last_action(session, settings, second_id)
        relationship = session.get(CandidateRelationship, relationship_id)
        assert session.get(QuestionCandidate, second_id).status == "review"
        assert relationship.decision == "review"
        assert relationship.status == "pending"


def test_manual_merge_creates_auditable_relationship_and_undo_removes_it(tmp_path: Path) -> None:
    database, settings, _, _, first_id, second_id, relationship_id = _seed(tmp_path)
    with database.session_factory() as session:
        session.delete(session.get(CandidateRelationship, relationship_id))
        session.commit()
        first_stem = session.get(QuestionCandidate, first_id).stem
        second_status = session.get(QuestionCandidate, second_id).status

        merged = merge_candidates(session, first_id, second_id)
        relationship = session.scalar(
            select(CandidateRelationship).where(
                CandidateRelationship.relation_type == "manual_merge",
                CandidateRelationship.primary_candidate_id == first_id,
                CandidateRelationship.secondary_candidate_id == second_id,
            )
        )
        assert merged.status == "review"
        assert relationship is not None
        assert relationship.status == "resolved"
        assert relationship.decision == "confirmed"
        relationship_id = relationship.id

        restored = undo_last_action(session, settings, first_id)
        assert restored.stem == first_stem
        assert session.get(QuestionCandidate, second_id).status == second_status
        assert session.get(CandidateRelationship, relationship_id) is None


def test_manual_merge_reuses_existing_relationship_when_id_is_omitted(tmp_path: Path) -> None:
    database, _, _, _, first_id, second_id, relationship_id = _seed(tmp_path)
    with database.session_factory() as session:
        merge_candidates(session, first_id, second_id)

        relationships = list(session.scalars(select(CandidateRelationship)))
        assert [item.id for item in relationships] == [relationship_id]
        assert relationships[0].status == "resolved"


def test_manual_merge_rejects_cross_batch_candidate(tmp_path: Path) -> None:
    database, _, batch_id, _, first_id, second_id, _ = _seed(tmp_path)
    with database.session_factory() as session:
        batch = session.get(ImportBatch, batch_id)
        second = session.get(QuestionCandidate, second_id)
        other_batch = ImportBatch(
            course_id=batch.course_id,
            name="其它批次",
            source_relative_path="课程/其它批次",
            status="question_review",
        )
        session.add(other_batch)
        session.flush()
        second.batch_id = other_batch.id
        session.commit()

        with pytest.raises(ReviewValidationError, match="同一批次"):
            merge_candidates(session, first_id, second_id)


@pytest.mark.parametrize("inactive_status", sorted(INACTIVE_CANDIDATE_STATUSES))
@pytest.mark.parametrize(
    "operation",
    ["merge_primary", "merge_secondary", "duplicate_candidate", "duplicate_target"],
)
def test_manual_relationship_actions_reject_inactive_candidates_without_side_effects(
    tmp_path: Path,
    inactive_status: str,
    operation: str,
) -> None:
    database, _, _, _, first_id, second_id, relationship_id = _seed(tmp_path)
    with database.session_factory() as session:
        session.delete(session.get(CandidateRelationship, relationship_id))
        inactive_id = first_id if operation in {"merge_primary", "duplicate_candidate"} else second_id
        session.get(QuestionCandidate, inactive_id).status = inactive_status
        session.commit()

        with pytest.raises(ReviewValidationError, match="有效候选题"):
            if operation.startswith("merge"):
                merge_candidates(session, first_id, second_id)
            else:
                mark_duplicate(session, first_id, second_id)

        assert list(session.scalars(select(CandidateRelationship))) == []
        assert session.scalar(select(func.count(ReviewAction.id))) == 0


@pytest.mark.parametrize("with_relationship_id", [False, True])
def test_manual_merge_rejects_inactive_candidates_before_relationship_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_relationship_id: bool,
) -> None:
    database, _, _, _, first_id, second_id, relationship_id = _seed(tmp_path)
    with database.session_factory() as session:
        if not with_relationship_id:
            session.delete(session.get(CandidateRelationship, relationship_id))
        session.get(QuestionCandidate, second_id).status = "ignored"
        session.commit()

        serialized_pairs: list[tuple[str, str]] = []
        claimed_relationship_ids: list[str] = []
        serialize = actions._serialize_manual_merge_pair
        claim = actions.claim_pending_relationship

        def record_serialization(
            action_session: Session,
            primary_id: str,
            secondary_id: str,
        ) -> None:
            serialized_pairs.append((primary_id, secondary_id))
            serialize(action_session, primary_id, secondary_id)

        def record_claim(action_session: Session, claimed_id: str) -> CandidateRelationship:
            claimed_relationship_ids.append(claimed_id)
            return claim(action_session, claimed_id)

        monkeypatch.setattr(actions, "_serialize_manual_merge_pair", record_serialization)
        monkeypatch.setattr(actions, "claim_pending_relationship", record_claim)

        with pytest.raises(ReviewValidationError, match="有效候选题"):
            merge_candidates(
                session,
                first_id,
                second_id,
                relationship_id if with_relationship_id else None,
            )

        assert serialized_pairs == []
        assert claimed_relationship_ids == []
