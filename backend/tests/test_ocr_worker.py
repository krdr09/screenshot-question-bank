from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from threading import Barrier, Event

from PIL import Image
import pytest
from sqlalchemy import select

from app.config import Settings
from app.database import Database
from app.models import ImportBatch, QuestionCandidate, SourceImage
from app.ocr.base import OcrBox, OcrPageResult, Point
from app.ocr.mock import MockOcrEngine
from app.ocr.paddle import PaddleOcrEngine, RawOcrLine
from app.schemas.imports import ImportJobCreate
from app.services import import_service
from app.services.import_service import (
    ImportConflictError,
    UploadCandidate,
    create_import_job,
    import_images,
)
from app.services.backups.maintenance import MaintenanceCoordinator
from app.services.backups.errors import BackupBusyError
from app.workers.ocr_worker import PersistentOcrWorker


def png_bytes(color: tuple[int, int, int]) -> bytes:
    output = BytesIO()
    Image.new("RGB", (40, 60), color).save(output, format="PNG")
    return output.getvalue()


def result(text: str = "Question") -> OcrPageResult:
    return OcrPageResult(
        page_width=40,
        page_height=60,
        boxes=[
            OcrBox(
                text=text,
                polygon=[
                    Point(x=1, y=2),
                    Point(x=30, y=2),
                    Point(x=30, y=12),
                    Point(x=1, y=12),
                ],
                confidence=0.97,
            )
        ],
        raw_output={"mock": True},
    )


def setup_database(tmp_path: Path) -> tuple[Settings, Database]:
    settings = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        auto_create_schema=True,
        start_background_worker=False,
        frontend_dist=tmp_path / "missing-dist",
        _env_file=None,
    )
    database = Database(settings)
    database.create_schema()
    return settings, database


def create_batch(
    database: Database,
    settings: Settings,
    name: str,
    image_names: list[str],
) -> str:
    with database.session_factory() as session:
        batch = create_import_job(
            session,
            ImportJobCreate(
                course_name="运筹学",
                category_path="第一章",
                batch_name=name,
                source_relative_path=f"运筹学/第一章/{name}",
            ),
        )
        import_images(
            session,
            settings,
            batch,
            [
                UploadCandidate(
                    original_name=image_name,
                    source_relative_path=f"运筹学/第一章/{name}/{image_name}",
                    content=png_bytes((20 + index, 60, 90)),
                )
                for index, image_name in enumerate(image_names)
            ],
        )
        return batch.id


def test_concurrent_uploads_allocate_distinct_order_and_preserve_count(
    tmp_path: Path, monkeypatch
) -> None:
    settings, database = setup_database(tmp_path)
    with database.session_factory() as session:
        batch = create_import_job(
            session,
            ImportJobCreate(
                course_name="运筹学",
                category_path="并发",
                batch_name="同时上传",
                source_relative_path="运筹学/并发/同时上传",
            ),
        )
        batch_id = batch.id

    validation_barrier = Barrier(2)
    original_validate_candidate = import_service.validate_candidate

    def synchronize_validation(candidate, current_settings):
        validated = original_validate_candidate(candidate, current_settings)
        validation_barrier.wait(timeout=5)
        return validated

    monkeypatch.setattr(
        import_service, "validate_candidate", synchronize_validation
    )

    def upload(name: str, color: tuple[int, int, int]):
        with database.session_factory() as session:
            batch = session.get(ImportBatch, batch_id)
            assert batch is not None
            return import_images(
                session,
                settings,
                batch,
                [
                    UploadCandidate(
                        original_name=name,
                        source_relative_path=f"同时上传/{name}",
                        content=png_bytes(color),
                    )
                ],
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(upload, "one.png", (10, 20, 30)),
            executor.submit(upload, "two.png", (40, 50, 60)),
        ]
        results = [future.result(timeout=10) for future in futures]

    with database.session_factory() as session:
        batch = session.get(ImportBatch, batch_id)
        images = list(
            session.scalars(
                select(SourceImage)
                .where(SourceImage.batch_id == batch_id)
                .order_by(SourceImage.order_index)
            )
        )
        assert batch is not None
        state = (
            batch.status,
            batch.uploaded_count,
            [image.order_index for image in images],
        )
    database.dispose()

    assert [result.uploaded_count for result in results] == [1, 1]
    assert state == ("uploading", 2, [0, 1])


def test_enqueue_wins_before_blocked_upload_rechecks_batch_status(
    tmp_path: Path, monkeypatch
) -> None:
    settings, database = setup_database(tmp_path)
    batch_id = create_batch(database, settings, "排队边界", ["existing.png"])
    worker = PersistentOcrWorker(
        database, settings, MockOcrEngine(default=result())
    )
    validation_started = Event()
    allow_validation_to_finish = Event()
    original_validate_candidate = import_service.validate_candidate

    def block_validation(candidate, current_settings):
        validated = original_validate_candidate(candidate, current_settings)
        validation_started.set()
        if not allow_validation_to_finish.wait(timeout=5):
            raise AssertionError("上传校验未被测试线程释放")
        return validated

    monkeypatch.setattr(import_service, "validate_candidate", block_validation)

    def upload_late_image():
        with database.session_factory() as session:
            batch = session.get(ImportBatch, batch_id)
            assert batch is not None
            return import_images(
                session,
                settings,
                batch,
                [
                    UploadCandidate(
                        original_name="late.png",
                        source_relative_path="排队边界/late.png",
                        content=png_bytes((90, 80, 70)),
                    )
                ],
            )

    upload_error: Exception | None = None
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(upload_late_image)
        assert validation_started.wait(timeout=5)
        try:
            worker.enqueue(batch_id)
        finally:
            allow_validation_to_finish.set()
        try:
            future.result(timeout=10)
        except Exception as error:
            upload_error = error

    with database.session_factory() as session:
        batch = session.get(ImportBatch, batch_id)
        images = list(
            session.scalars(
                select(SourceImage).where(SourceImage.batch_id == batch_id)
            )
        )
        assert batch is not None
        state = (batch.status, batch.uploaded_count, len(images))
    database.dispose()

    assert isinstance(upload_error, ImportConflictError)
    assert state == ("queued", 1, 1)


@pytest.mark.parametrize("terminal_status", ["failed", "partially_ready", "cancelled"])
def test_enqueue_rejects_terminal_batch_without_mutation(
    tmp_path: Path, terminal_status: str
) -> None:
    settings, database = setup_database(tmp_path)
    batch_id = create_batch(database, settings, "终态门禁", ["page.png"])
    worker = PersistentOcrWorker(
        database, settings, MockOcrEngine(default=result())
    )

    with database.session_factory() as session:
        batch = session.get(ImportBatch, batch_id)
        assert batch is not None
        image = batch.images[0]
        batch.status = terminal_status
        batch.processed_count = 1
        batch.error_count = 0 if terminal_status == "cancelled" else 1
        batch.cancel_requested = terminal_status == "cancelled"
        batch.current_file = None
        batch.ocr_device = "cpu"
        batch.error_log_json = (
            []
            if terminal_status == "cancelled"
            else [{"file": "page.png", "message": "原始失败"}]
        )
        image.ocr_status = "pending" if terminal_status == "cancelled" else "failed"
        image.ocr_error = None if terminal_status == "cancelled" else "原始失败"
        session.commit()
        before = (
            batch.status,
            batch.processed_count,
            batch.error_count,
            batch.cancel_requested,
            batch.current_file,
            batch.ocr_device,
            list(batch.error_log_json),
            image.ocr_status,
            image.ocr_error,
        )

    with pytest.raises(ValueError) as captured:
        worker.enqueue(batch_id)

    assert str(captured.value) == (
        f"导入任务当前状态不允许处理：{terminal_status}"
    )
    assert worker.run_next() is None
    with database.session_factory() as session:
        batch = session.get(ImportBatch, batch_id)
        assert batch is not None
        image = batch.images[0]
        after = (
            batch.status,
            batch.processed_count,
            batch.error_count,
            batch.cancel_requested,
            batch.current_file,
            batch.ocr_device,
            list(batch.error_log_json),
            image.ocr_status,
            image.ocr_error,
        )
    database.dispose()

    assert after == before


def test_worker_persists_raw_ocr_after_every_image(tmp_path: Path) -> None:
    settings, database = setup_database(tmp_path)
    batch_id = create_batch(database, settings, "作业1", ["01.png", "02.png"])
    worker = PersistentOcrWorker(database, settings, MockOcrEngine(default=result()))

    worker.enqueue(batch_id)
    assert worker.run_next() == batch_id

    with database.session_factory() as session:
        batch = session.get(ImportBatch, batch_id)
        images = list(
            session.scalars(
                select(SourceImage)
                .where(SourceImage.batch_id == batch_id)
                .order_by(SourceImage.order_index)
            )
        )
        assert batch is not None
        assert batch.status == "ordering_review"
        assert batch.processed_count == 2
        assert batch.current_file is None
        assert [image.ocr_status for image in images] == ["completed", "completed"]
        assert all(image.ocr_result_json["raw_output"] == {"mock": True} for image in images)
        assert [image.ocr_text for image in images] == ["Question", "Question"]
    database.dispose()


def test_worker_leaves_queued_batch_untouched_during_exclusive_maintenance(
    tmp_path: Path,
) -> None:
    settings, database = setup_database(tmp_path)
    batch_id = create_batch(database, settings, "维护互斥", ["01.png"])
    maintenance = MaintenanceCoordinator()
    worker = PersistentOcrWorker(
        database,
        settings,
        MockOcrEngine(default=result()),
        maintenance=maintenance,
    )
    worker.enqueue(batch_id)

    with maintenance.exclusive("创建备份"):
        assert worker.run_next() is None
    with database.session_factory() as session:
        batch = session.get(ImportBatch, batch_id)
        assert batch is not None
        assert batch.status == "queued"
        assert batch.processed_count == 0

    assert worker.run_next() == batch_id
    database.dispose()


def test_source_reprocess_refuses_exclusive_maintenance(tmp_path: Path) -> None:
    settings, database = setup_database(tmp_path)
    batch_id = create_batch(database, settings, "重新识别互斥", ["01.png"])
    with database.session_factory() as session:
        image_id = session.scalar(
            select(SourceImage.id).where(SourceImage.batch_id == batch_id)
        )
    assert image_id is not None
    maintenance = MaintenanceCoordinator()
    engine = MockOcrEngine(default=result())
    worker = PersistentOcrWorker(
        database,
        settings,
        engine,
        maintenance=maintenance,
    )

    with maintenance.exclusive("创建备份"):
        with pytest.raises(BackupBusyError):
            worker.reprocess_image(image_id)

    assert engine.calls == []
    database.dispose()


def test_worker_skips_sha256_duplicate_source_without_second_ocr(tmp_path: Path) -> None:
    settings, database = setup_database(tmp_path)
    content = png_bytes((25, 60, 90))
    with database.session_factory() as session:
        batch = create_import_job(
            session,
            ImportJobCreate(
                course_name="运筹学",
                category_path="第一章",
                batch_name="完全重复",
                source_relative_path="运筹学/第一章/完全重复",
            ),
        )
        import_result = import_images(
            session,
            settings,
            batch,
            [
                UploadCandidate("one.png", "完全重复/one.png", content),
                UploadCandidate("copy.png", "完全重复/copy.png", content),
            ],
        )
        assert (
            import_result.images[1].duplicate_of_id == import_result.images[0].id
        )
        batch_id = batch.id
    engine = MockOcrEngine(default=result())
    worker = PersistentOcrWorker(database, settings, engine)

    worker.enqueue(batch_id)
    worker.run_next()

    with database.session_factory() as session:
        batch = session.get(ImportBatch, batch_id)
        images = list(session.scalars(select(SourceImage).where(SourceImage.batch_id == batch_id).order_by(SourceImage.order_index)))
        assert batch is not None and batch.processed_count == 2
        assert [image.ocr_status for image in images] == ["completed", "duplicate"]
        assert images[1].review_flag == "duplicate"
    assert len(engine.calls) == 1
    database.dispose()


def test_worker_recovers_processing_batch_after_restart(tmp_path: Path) -> None:
    settings, database = setup_database(tmp_path)
    batch_id = create_batch(database, settings, "作业1", ["01.png"])
    with database.session_factory() as session:
        batch = session.get(ImportBatch, batch_id)
        assert batch is not None
        batch.status = "processing"
        batch.current_file = "01.png"
        session.commit()

    worker = PersistentOcrWorker(database, settings, MockOcrEngine(default=result()))

    assert worker.recover_interrupted_jobs() == 1
    assert worker.run_next() == batch_id
    with database.session_factory() as session:
        batch = session.get(ImportBatch, batch_id)
        assert batch is not None
        assert batch.status == "question_review"
        assert batch.processed_count == 1
    database.dispose()


def test_cancelling_one_batch_does_not_cancel_another(tmp_path: Path) -> None:
    settings, database = setup_database(tmp_path)
    cancelled_id = create_batch(database, settings, "取消批次", ["cancel.png"])
    active_id = create_batch(database, settings, "正常批次", ["active.png"])
    worker = PersistentOcrWorker(database, settings, MockOcrEngine(default=result()))
    worker.enqueue(cancelled_id)
    worker.enqueue(active_id)

    worker.request_cancel(cancelled_id)
    assert worker.run_next() == cancelled_id
    assert worker.run_next() == active_id

    with database.session_factory() as session:
        cancelled = session.get(ImportBatch, cancelled_id)
        active = session.get(ImportBatch, active_id)
        assert cancelled is not None and cancelled.status == "cancelled"
        assert cancelled.processed_count == 0
        assert active is not None and active.status == "ordering_review"
        assert active.processed_count == 1
    database.dispose()


def test_crop_failure_marks_image_failed_instead_of_stopping_worker(
    tmp_path: Path,
) -> None:
    settings, database = setup_database(tmp_path)
    batch_id = create_batch(database, settings, "尺寸异常", ["page.png"])
    mismatched = OcrPageResult(
        page_width=40,
        page_height=999,
        boxes=[
            OcrBox(
                text="1.[单选题]",
                polygon=[
                    Point(x=1, y=2),
                    Point(x=30, y=2),
                    Point(x=30, y=12),
                    Point(x=1, y=12),
                ],
                confidence=0.97,
            ),
            OcrBox(
                text="A. 甲",
                polygon=[Point(x=1, y=20), Point(x=30, y=20), Point(x=30, y=30), Point(x=1, y=30)],
                confidence=0.97,
            ),
            OcrBox(
                text="B. 乙",
                polygon=[Point(x=1, y=35), Point(x=30, y=35), Point(x=30, y=45), Point(x=1, y=45)],
                confidence=0.97,
            ),
            OcrBox(
                text="正确答案:A",
                polygon=[Point(x=1, y=50), Point(x=30, y=50), Point(x=30, y=60), Point(x=1, y=60)],
                confidence=0.97,
            ),
        ],
        raw_output={"mismatched": True},
    )
    worker = PersistentOcrWorker(database, settings, MockOcrEngine(default=mismatched))
    worker.enqueue(batch_id)

    assert worker.run_next() == batch_id

    with database.session_factory() as session:
        batch = session.get(ImportBatch, batch_id)
        image = session.scalar(select(SourceImage).where(SourceImage.batch_id == batch_id))
        assert batch is not None and batch.status == "failed"
        assert batch.error_count == 1
        assert image is not None and image.ocr_status == "failed"
        assert "页面高度与原图不一致" in (image.ocr_error or "")
    database.dispose()


def test_partial_crop_write_marks_failure_without_orphan_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings, database = setup_database(tmp_path)
    batch_id = create_batch(database, settings, "裁切写入中断", ["page.png"])
    original_save = Image.Image.save

    def fail_crop_save(self, fp, *args, **kwargs):
        path = Path(fp)
        if "crops" in path.parts:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"partial")
            raise OSError("crop write interrupted")
        return original_save(self, fp, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "save", fail_crop_save)
    worker = PersistentOcrWorker(
        database,
        settings,
        MockOcrEngine(default=result("1.[单选题] 题干")),
    )
    worker.enqueue(batch_id)

    assert worker.run_next() == batch_id

    with database.session_factory() as session:
        batch = session.get(ImportBatch, batch_id)
        image = session.scalar(
            select(SourceImage).where(SourceImage.batch_id == batch_id)
        )
        candidates = list(
            session.scalars(
                select(QuestionCandidate).where(
                    QuestionCandidate.batch_id == batch_id
                )
            )
        )
        assert batch is not None
        assert image is not None
        assert batch.status == "failed"
        assert batch.error_count == 1
        assert image.ocr_status == "failed"
        assert "crop write interrupted" in (image.ocr_error or "")
        assert candidates == []

    crop_root = settings.data_dir / "crops"
    assert [path for path in crop_root.rglob("*") if path.is_file()] == []
    database.dispose()


def test_worker_persists_device_after_engine_runtime_fallback(tmp_path: Path) -> None:
    class SwitchingEngine:
        device = "gpu"

        def recognize(self, image_path: Path) -> OcrPageResult:
            self.device = "cpu"
            return result()

    settings, database = setup_database(tmp_path)
    batch_id = create_batch(database, settings, "设备回退", ["page.png"])
    worker = PersistentOcrWorker(database, settings, SwitchingEngine())
    worker.enqueue(batch_id)

    assert worker.run_next() == batch_id

    with database.session_factory() as session:
        batch = session.get(ImportBatch, batch_id)
        assert batch is not None and batch.ocr_device == "cpu"
    database.dispose()


def test_successful_cuda_probe_fallback_is_not_an_import_error(tmp_path: Path) -> None:
    class ProbeFailingBackend:
        def cuda_available(self) -> bool:
            raise RuntimeError("CUDA 探测失败")

        def create_engine(self, use_gpu: bool):
            assert use_gpu is False
            return {"gpu": False}

        def smoke_test(self, engine) -> None:
            return None

        def recognize(self, engine, image_path: Path) -> list[RawOcrLine]:
            texts = [
                "1.[单选题]",
                "题干",
                "A. 甲",
                "B. 乙",
                "正确答案:A",
            ]
            return [
                RawOcrLine(
                    text=text,
                    confidence=0.98,
                    polygon=[[1, y], [38, y], [38, y + 6], [1, y + 6]],
                )
                for text, y in zip(texts, [2, 12, 22, 32, 42], strict=True)
            ]

    settings, database = setup_database(tmp_path)
    batch_id = create_batch(database, settings, "探测回退", ["page.png"])
    engine = PaddleOcrEngine("auto", ProbeFailingBackend())
    worker = PersistentOcrWorker(database, settings, engine)
    worker.enqueue(batch_id)

    assert worker.run_next() == batch_id

    with database.session_factory() as session:
        batch = session.get(ImportBatch, batch_id)
        assert batch is not None
        assert batch.ocr_device == "cpu"
        assert batch.error_count == 0
        assert batch.error_log_json == []
    database.dispose()
