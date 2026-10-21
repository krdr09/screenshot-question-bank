from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.config import Settings
from app.main import create_app
from app.models import (
    Attempt,
    CandidateImagePart,
    Category,
    Course,
    ImportBatch,
    PracticeItem,
    PracticeSession,
    QuestionCandidate,
    SourceImage,
    WrongBookEntry,
)
from app.ocr.mock import MockOcrEngine
from app.services.library.batch_delete import delete_import_batch
from app.services.library.deletion_recovery import BatchDeletionJournal
from app.services.practice import sessions as practice_sessions
from app.services.practice.selection import PracticeSelectionError, SelectionCriteria
from app.services.review.editor import delete_candidate


def _seed_practice_lifecycle_batch(session, settings: Settings) -> tuple[str, Path, bytes]:
    course = Course(name="lifecycle course")
    category = Category(course=course, path="lifecycle category")
    batch = ImportBatch(
        course=course,
        category=category,
        name="lifecycle batch",
        source_relative_path="lifecycle/course/batch",
        status="ready",
    )
    session.add(batch)
    session.flush()
    source = SourceImage(
        batch=batch,
        source_relative_path="history.png",
        original_name="history.png",
        safe_name="history.png",
        relative_path="imports/history.png",
        sha256="c" * 64,
        mime_type="image/png",
        width=20,
        height=20,
        file_size=1,
        order_index=0,
        ocr_status="completed",
    )
    question = QuestionCandidate(
        batch_id=batch.id,
        category_id=category.id,
        source_image=source,
        source_display_number="1",
        question_type="single_choice",
        stem="lifecycle question",
        options_json=[{"label": "A", "text": "one"}],
        correct_answer_json="A",
        raw_ocr_text="lifecycle question",
        normalized_ocr_text="lifecycle question",
        ocr_confidence=1,
        parse_confidence=1,
        display_mode="image_preferred",
        anomaly_flags_json=[],
        status="ready",
    )
    session.add_all([source, question])
    session.flush()
    crop = settings.data_dir / "crops/history.png"
    crop.parent.mkdir(parents=True, exist_ok=True)
    crop.write_bytes(b"original lifecycle crop bytes")
    session.add(
        CandidateImagePart(
            candidate_id=question.id,
            source_image_id=source.id,
            crop_path="crops/history.png",
            source_y_start=0,
            source_y_end=20,
            part_index=0,
        )
    )
    session.commit()
    return batch.id, crop, crop.read_bytes()


def test_batch_delete_waits_for_session_creation_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        auto_create_schema=True,
        start_background_worker=False,
        frontend_dist=tmp_path / "missing",
        _env_file=None,
    )
    app = create_app(settings, MockOcrEngine())
    with TestClient(app):
        database = app.state.database
        with database.session_factory() as session:
            target_id, _crop, original_crop_bytes = _seed_practice_lifecycle_batch(
                session, settings
            )

        saved_prepare = BatchDeletionJournal.prepare

        def recording_prepare(self, batch_id, relative_paths):
            delete_reached_prepare.set()
            return saved_prepare(self, batch_id, relative_paths)

        monkeypatch.setattr(BatchDeletionJournal, "prepare", recording_prepare)

        def create_worker() -> list[dict]:
            with database.session_factory() as session:
                saved_commit = session.commit

                def blocking_commit() -> None:
                    create_at_commit.set()
                    assert allow_create_commit.wait(5)
                    saved_commit()

                session.commit = blocking_commit
                created = practice_sessions.create_practice_session(
                    session,
                    SelectionCriteria(batch_id=target_id),
                    {"batch_id": target_id},
                    1,
                    "sequential",
                    "deferred",
                )
                return list(created.items[0].image_parts_snapshot_json)

        def delete_worker() -> None:
            delete_called.set()
            with database.session_factory() as session:
                saved_get = session.get

                def recording_get(*args, **kwargs):
                    delete_reached_first_get.set()
                    return saved_get(*args, **kwargs)

                session.get = recording_get
                delete_import_batch(session, settings, target_id)

        create_at_commit = Event()
        allow_create_commit = Event()
        delete_reached_first_get = Event()
        delete_reached_prepare = Event()
        delete_called = Event()
        with ThreadPoolExecutor(max_workers=2) as pool:
            create_future = pool.submit(create_worker)
            assert create_at_commit.wait(2)
            delete_future = pool.submit(delete_worker)
            assert delete_called.wait(2)
            try:
                assert not delete_reached_first_get.wait(0.2)
                assert not delete_reached_prepare.wait(0.2)
            finally:
                allow_create_commit.set()
            created_snapshot = create_future.result(timeout=5)
            delete_future.result(timeout=5)

        assert created_snapshot[0]["crop_path"] == "crops/history.png"
        assert (settings.data_dir / "crops/history.png").read_bytes() == original_crop_bytes


def test_session_creation_waits_for_batch_deletion_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        auto_create_schema=True,
        start_background_worker=False,
        frontend_dist=tmp_path / "missing",
        _env_file=None,
    )
    app = create_app(settings, MockOcrEngine())
    with TestClient(app):
        database = app.state.database
        with database.session_factory() as session:
            target_id, crop, _original_crop_bytes = _seed_practice_lifecycle_batch(
                session, settings
            )

        saved_stage = BatchDeletionJournal.stage
        saved_select_questions = practice_sessions.select_questions

        def blocking_stage(self, operation) -> None:
            saved_stage(self, operation)
            delete_in_stage.set()
            assert allow_delete.wait(5)

        def recording_select_questions(*args, **kwargs):
            create_reached_selection.set()
            return saved_select_questions(*args, **kwargs)

        monkeypatch.setattr(BatchDeletionJournal, "stage", blocking_stage)
        monkeypatch.setattr(
            practice_sessions, "select_questions", recording_select_questions
        )

        def delete_worker() -> None:
            with database.session_factory() as session:
                delete_import_batch(session, settings, target_id)

        def create_worker() -> None:
            create_called.set()
            with database.session_factory() as session:
                practice_sessions.create_practice_session(
                    session,
                    SelectionCriteria(batch_id=target_id),
                    {"batch_id": target_id},
                    1,
                    "sequential",
                    "deferred",
                )

        delete_in_stage = Event()
        allow_delete = Event()
        create_reached_selection = Event()
        create_called = Event()
        with ThreadPoolExecutor(max_workers=2) as pool:
            delete_future = pool.submit(delete_worker)
            assert delete_in_stage.wait(2)
            create_future = pool.submit(create_worker)
            assert create_called.wait(2)
            try:
                assert not create_reached_selection.wait(0.2)
            finally:
                allow_delete.set()
            delete_future.result(timeout=5)
            with pytest.raises(PracticeSelectionError):
                create_future.result(timeout=5)

        with database.session_factory() as session:
            assert session.query(PracticeSession).count() == 0
            assert session.query(PracticeItem).count() == 0
        assert not crop.exists()


def test_session_creation_waits_for_batch_deletion_cleanup_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        auto_create_schema=True,
        start_background_worker=False,
        frontend_dist=tmp_path / "missing",
        _env_file=None,
    )
    app = create_app(settings, MockOcrEngine())
    with TestClient(app):
        database = app.state.database
        with database.session_factory() as session:
            target_id, crop, _original_crop_bytes = _seed_practice_lifecycle_batch(
                session, settings
            )

        saved_cleanup = BatchDeletionJournal.cleanup
        saved_select_questions = practice_sessions.select_questions

        class BlockingCleanupResult:
            def __init__(self, value: bool) -> None:
                self.value = value

            def __bool__(self) -> bool:
                delete_reached_cleanup_result.set()
                assert allow_cleanup_result.wait(5)
                return self.value

        def recording_cleanup(self, operation):
            return BlockingCleanupResult(saved_cleanup(self, operation))

        def recording_select_questions(*args, **kwargs):
            create_reached_selection.set()
            return saved_select_questions(*args, **kwargs)

        monkeypatch.setattr(BatchDeletionJournal, "cleanup", recording_cleanup)
        monkeypatch.setattr(
            practice_sessions, "select_questions", recording_select_questions
        )

        def delete_worker() -> None:
            delete_called.set()
            with database.session_factory() as session:
                delete_import_batch(session, settings, target_id)

        def create_worker() -> None:
            create_called.set()
            with database.session_factory() as session:
                practice_sessions.create_practice_session(
                    session,
                    SelectionCriteria(batch_id=target_id),
                    {"batch_id": target_id},
                    1,
                    "sequential",
                    "deferred",
                )

        delete_reached_cleanup_result = Event()
        allow_cleanup_result = Event()
        create_reached_selection = Event()
        delete_called = Event()
        create_called = Event()
        with ThreadPoolExecutor(max_workers=2) as pool:
            delete_future = pool.submit(delete_worker)
            try:
                assert delete_called.wait(2)
                assert delete_reached_cleanup_result.wait(2)
                create_future = pool.submit(create_worker)
                assert create_called.wait(2)
                assert not create_reached_selection.wait(0.2)
            finally:
                allow_cleanup_result.set()
            delete_future.result(timeout=5)
            with pytest.raises(PracticeSelectionError):
                create_future.result(timeout=5)

        with database.session_factory() as session:
            assert session.query(PracticeSession).count() == 0
            assert session.query(PracticeItem).count() == 0
        assert not crop.exists()


def test_question_and_batch_deletion_keep_practice_history(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        auto_create_schema=True,
        start_background_worker=False,
        frontend_dist=tmp_path / "missing",
        _env_file=None,
    )
    app = create_app(settings, MockOcrEngine())
    with TestClient(app):
        with app.state.database.session_factory() as session:
            course = Course(name="删除兼容课程")
            category = Category(course=course, path="第一章")
            batch = ImportBatch(
                course=course,
                category=category,
                name="删除兼容批次",
                source_relative_path="删除兼容课程/第一章/删除兼容批次",
                status="ready",
            )
            session.add(batch)
            session.flush()
            image = SourceImage(
                batch=batch,
                source_relative_path="one.png",
                original_name="one.png",
                safe_name="one.png",
                relative_path="imports/one.png",
                sha256="a" * 64,
                mime_type="image/png",
                width=100,
                height=100,
                file_size=1,
                order_index=0,
                ocr_status="completed",
            )
            question = QuestionCandidate(
                batch_id=batch.id,
                category_id=category.id,
                source_image=image,
                source_display_number="1",
                question_type="single_choice",
                stem="需要保留历史的题目",
                options_json=[
                    {"label": "A", "text": "甲"},
                    {"label": "B", "text": "乙"},
                ],
                correct_answer_json="A",
                raw_ocr_text="OCR",
                normalized_ocr_text="ocr",
                ocr_confidence=1,
                parse_confidence=1,
                display_mode="text",
                anomaly_flags_json=[],
                status="ready",
            )
            session.add_all([image, question])
            session.flush()
            practice = PracticeSession(
                feedback_mode="immediate",
                order_mode="sequential",
                selection_json={"batch_id": batch.id},
                requested_count=1,
                actual_count=1,
                limit_adjusted=False,
                status="submitted",
            )
            item = PracticeItem(
                session=practice,
                question_id=question.id,
                order_index=0,
                question_snapshot_json={
                    "question_type": "single_choice",
                    "stem": question.stem,
                    "options": question.options_json,
                },
                answer_snapshot_json="A",
                explanation_snapshot=None,
                image_parts_snapshot_json=[],
            )
            attempt = Attempt(
                session=practice,
                item=item,
                selected_answer_json="B",
                is_correct=False,
                was_unanswered=False,
            )
            wrong = WrongBookEntry(question_id=question.id)
            session.add_all([practice, item, attempt, wrong])
            session.commit()
            item_id = item.id
            attempt_id = attempt.id
            wrong_id = wrong.id
            question_id = question.id
            batch_id = batch.id

            delete_candidate(session, question_id)

            assert session.get(WrongBookEntry, wrong_id) is None
            assert session.get(PracticeItem, item_id).question_id == question_id

            delete_import_batch(session, settings, batch_id)

            historical_item = session.get(PracticeItem, item_id)
            historical_attempt = session.get(Attempt, attempt_id)
            assert historical_item is not None
            assert historical_item.question_id is None
            assert historical_attempt is not None
            assert historical_attempt.is_correct is False


@pytest.mark.parametrize("submitted", [False, True], ids=["active", "submitted"])
def test_batch_delete_keeps_practice_snapshot_crop_bytes(
    tmp_path: Path,
    submitted: bool,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        auto_create_schema=True,
        start_background_worker=False,
        frontend_dist=tmp_path / "missing",
        _env_file=None,
    )
    app = create_app(settings, MockOcrEngine())
    with TestClient(app) as client:
        with app.state.database.session_factory() as session:
            course = Course(name="snapshot course")
            category = Category(course=course, path="snapshot category")
            batch = ImportBatch(
                course=course,
                category=category,
                name="snapshot batch",
                source_relative_path="snapshot/course/batch",
                status="ready",
            )
            session.add(batch)
            session.flush()
            source = SourceImage(
                batch=batch,
                source_relative_path="history.png",
                original_name="history.png",
                safe_name="history.png",
                relative_path="imports/history.png",
                sha256="b" * 64,
                mime_type="image/png",
                width=100,
                height=100,
                file_size=1,
                order_index=0,
                ocr_status="completed",
            )
            question = QuestionCandidate(
                batch_id=batch.id,
                category_id=category.id,
                source_image=source,
                source_display_number="1",
                question_type="single_choice",
                stem="snapshot question",
                options_json=[{"label": "A", "text": "one"}],
                correct_answer_json="A",
                raw_ocr_text="snapshot question",
                normalized_ocr_text="snapshot question",
                ocr_confidence=1,
                parse_confidence=1,
                display_mode="image_preferred",
                anomaly_flags_json=[],
                status="ready",
            )
            session.add_all([source, question])
            session.flush()
            crop = settings.data_dir / "crops/history.png"
            crop.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (20, 20), "purple").save(crop)
            session.add(
                CandidateImagePart(
                    candidate_id=question.id,
                    source_image_id=source.id,
                    crop_path="crops/history.png",
                    source_y_start=0,
                    source_y_end=20,
                    part_index=0,
                )
            )
            session.commit()
            batch_id = batch.id
            question_id = question.id

        created = client.post(
            "/api/practice-sessions",
            json={
                "selection": {"batch_id": batch_id},
                "count": 1,
                "order_mode": "sequential",
                "feedback_mode": "deferred",
            },
        )
        assert created.status_code == 201, created.text
        payload = created.json()
        part_url = payload["items"][0]["image_parts"][0]["url"]
        if submitted:
            submitted_response = client.post(f"/api/practice-sessions/{payload['id']}/submit")
            assert submitted_response.status_code == 200, submitted_response.text

        before = client.get(part_url)
        assert before.status_code == 200
        with app.state.database.session_factory() as session:
            delete_import_batch(session, settings, batch_id)

        after = client.get(part_url)
        assert after.status_code == 200
        assert after.content == before.content
        assert (settings.data_dir / "crops/history.png").is_file()
        with app.state.database.session_factory() as session:
            assert session.get(ImportBatch, batch_id) is None
            assert session.get(QuestionCandidate, question_id) is None
            item = session.get(PracticeItem, payload["items"][0]["id"])
            assert item is not None
            assert item.question_id is None
            if submitted:
                assert item.attempt is not None
