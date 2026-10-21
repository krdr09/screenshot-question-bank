from pathlib import Path

from app.config import Settings
from app.database import Database
from app.models import (
    CandidateImagePart,
    Category,
    Course,
    ImportBatch,
    QuestionCandidate,
    SourceImage,
    WrongBookEntry,
)
from app.services.library.query import (
    LibraryFilters,
    get_question,
    list_library_metadata,
    list_questions,
)


def _database(tmp_path: Path) -> Database:
    database = Database(
        Settings(
            data_dir=tmp_path / "data",
            database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
            auto_create_schema=True,
            start_background_worker=False,
            frontend_dist=tmp_path / "missing",
            _env_file=None,
        )
    )
    database.create_schema()
    return database


def _candidate(batch: ImportBatch, image: SourceImage, stem: str, status: str) -> QuestionCandidate:
    return QuestionCandidate(
        batch_id=batch.id,
        source_image=image,
        category_id=batch.category_id,
        question_type="single_choice",
        stem=stem,
        options_json=[{"label": "A", "text": "甲"}, {"label": "B", "text": "乙"}],
        correct_answer_json="A",
        raw_ocr_text=f"OCR {stem}",
        normalized_ocr_text=f"ocr {stem}",
        ocr_confidence=0.98,
        parse_confidence=0.97,
        display_mode="text_first",
        anomaly_flags_json=[],
        status=status,
    )


def _seed(session):
    course = Course(name="医学")
    respiratory = Category(course=course, path="内科/呼吸")
    circulation = Category(course=course, path="内科/循环")
    other_course = Course(name="法规")
    other_category = Category(course=other_course, path="第一章")
    first_batch = ImportBatch(course=course, category=respiratory, name="第一批", source_relative_path="医学/第一批")
    second_batch = ImportBatch(course=course, category=circulation, name="第二批", source_relative_path="医学/第二批")
    other_batch = ImportBatch(course=other_course, category=other_category, name="法规批次", source_relative_path="法规/第一章")
    session.add_all([first_batch, second_batch, other_batch])
    session.flush()

    images = []
    for index, batch in enumerate((first_batch, second_batch, other_batch), start=1):
        image = SourceImage(
            batch=batch,
            source_relative_path=f"{index}.png",
            original_name=f"{index}.png",
            safe_name=f"{index}.png",
            relative_path=f"imports/{index}.png",
            sha256=str(index) * 64,
            mime_type="image/png",
            width=100,
            height=200,
            file_size=1,
            order_index=0,
            ocr_status="completed",
        )
        images.append(image)
    session.add_all(images)
    session.flush()

    pneumonia = _candidate(first_batch, images[0], "肺炎的首选治疗", "ready")
    pneumonia.edited_ocr_text = "肺炎 编辑 OCR"
    heart = _candidate(second_batch, images[1], "心脏传导", "ready")
    pending = _candidate(first_batch, images[0], "肺炎待检查", "review")
    regulation = _candidate(other_batch, images[2], "法规题", "ready")
    session.add_all([pneumonia, heart, pending, regulation])
    session.flush()
    session.add(WrongBookEntry(question_id=pneumonia.id))
    session.commit()
    return course, respiratory, first_batch, pneumonia, heart, pending, regulation


def test_library_query_only_returns_ready_questions_and_searches_ocr(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with database.session_factory() as session:
        course, respiratory, first_batch, pneumonia, _, pending, _ = _seed(session)

        result = list_questions(
            session,
            LibraryFilters(
                course_id=course.id,
                category_id=respiratory.id,
                batch_id=first_batch.id,
                question_type="single_choice",
                search="编辑 OCR",
                page=1,
                page_size=20,
            ),
        )

    assert result.total == 1
    assert [item.candidate.id for item in result.items] == [pneumonia.id]
    assert pending.id not in {item.candidate.id for item in result.items}
    assert result.items[0].category_path == "内科/呼吸"
    assert result.items[0].in_wrong_book is True


def test_library_query_filters_course_and_paginates_deterministically(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with database.session_factory() as session:
        course, _, _, pneumonia, heart, _, regulation = _seed(session)

        first_page = list_questions(session, LibraryFilters(course_id=course.id, page=1, page_size=1))
        second_page = list_questions(session, LibraryFilters(course_id=course.id, page=2, page_size=1))
        detail = get_question(session, heart.id)
        hidden = get_question(session, regulation.id, course_id=course.id)

    assert first_page.total == 2
    assert {first_page.items[0].candidate.id, second_page.items[0].candidate.id} == {pneumonia.id, heart.id}
    assert detail is not None and detail.in_wrong_book is False
    assert hidden is None


def test_batch_metadata_filters_and_counts_by_effective_category(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with database.session_factory() as session:
        course, respiratory, first_batch, pneumonia, heart, pending, _ = _seed(session)
        pneumonia.category_id = None
        moved = _candidate(first_batch, pneumonia.source_image, "moved ready candidate", "ready")
        moved.category_id = heart.category_id
        pending.category_id = heart.category_id
        deleted = _candidate(first_batch, pneumonia.source_image, "deleted candidate", "deleted")
        deleted.category_id = heart.category_id
        session.add_all([moved, deleted])
        session.commit()

        _, all_batches = list_library_metadata(session, course_id=course.id)
        _, respiratory_batches = list_library_metadata(
            session, course_id=course.id, category_id=respiratory.id
        )
        _, circulation_batches = list_library_metadata(
            session, course_id=course.id, category_id=heart.category_id
        )
        _, unknown_batches = list_library_metadata(
            session, course_id=course.id, category_id="missing-category"
        )

    all_first = next(item for item in all_batches if item.id == first_batch.id)
    respiratory_first = next(
        item for item in respiratory_batches if item.id == first_batch.id
    )
    circulation_first = next(
        item for item in circulation_batches if item.id == first_batch.id
    )
    assert all_first.question_count == 2
    assert respiratory_first.question_count == 1
    assert circulation_first.question_count == 1
    assert circulation_first.category_id == respiratory.id
    assert circulation_first.category_path == "内科/呼吸"
    assert unknown_batches == []
    database.dispose()


def test_library_excludes_ready_questions_with_flagged_primary_or_part(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    with database.session_factory() as session:
        course, _, first_batch, pneumonia, heart, _, regulation = _seed(session)
        pneumonia.source_image.review_flag = "legacy_nonempty_marker"
        flagged_part_source = SourceImage(
            batch_id=heart.batch_id,
            source_relative_path="flagged-part.png",
            original_name="flagged-part.png",
            safe_name="flagged-part.png",
            relative_path="imports/flagged-part.png",
            sha256="9" * 64,
            mime_type="image/png",
            width=100,
            height=200,
            file_size=1,
            review_flag="damaged",
            order_index=1,
            ocr_status="completed",
        )
        session.add(flagged_part_source)
        session.flush()
        session.add(
            CandidateImagePart(
                candidate_id=heart.id,
                source_image_id=flagged_part_source.id,
                crop_path="crops/flagged-part.png",
                source_y_start=0,
                source_y_end=100,
                part_index=0,
            )
        )
        session.commit()

        page = list_questions(session, LibraryFilters(course_id=course.id))
        primary_detail = get_question(session, pneumonia.id)
        part_detail = get_question(session, heart.id)
        _, batches = list_library_metadata(session, course_id=course.id)
        counts = {item.id: item.question_count for item in batches}
        _, filtered_batches = list_library_metadata(
            session,
            course_id=course.id,
            category_id=first_batch.category_id,
        )

    assert [item.candidate.id for item in page.items] == []
    assert primary_detail is None
    assert part_detail is None
    assert counts[first_batch.id] == 0
    assert counts[heart.batch_id] == 0
    assert first_batch.id not in {item.id for item in filtered_batches}
    assert regulation.id not in {item.candidate.id for item in page.items}
    database.dispose()
