import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models import (
    CandidateImagePart,
    CandidateRelationship,
    Course,
    ImportBatch,
    QuestionCandidate,
    SourceImage,
)


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def test_upgrade_head_records_current_revision(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "migration.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("QUESTION_PRACTICE_DATABASE_URL", database_url)
    ocr_logger = logging.getLogger("app.ocr.paddle")
    ocr_logger.disabled = False

    command.upgrade(Config(str(BACKEND_ROOT / "alembic.ini")), "head")

    assert ocr_logger.disabled is False

    with create_engine(database_url).connect() as connection:
        revision = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        batch_columns = {
            column["name"] for column in inspect(connection).get_columns("import_batches")
        }
        image_columns = {
            column["name"] for column in inspect(connection).get_columns("source_images")
        }
        table_names = set(inspect(connection).get_table_names())
        candidate_columns = {
            column["name"] for column in inspect(connection).get_columns("question_candidates")
        }
        wrong_columns = {
            column["name"]
            for column in inspect(connection).get_columns("wrong_book_entries")
        }
    assert revision == "20260807_0008"
    assert {"processed_count", "error_count", "cancel_requested"} <= batch_columns
    assert {"ocr_status", "ocr_result_json", "ocr_text", "review_flag"} <= image_columns
    assert "edited_ocr_text" in candidate_columns
    assert "category_id" in candidate_columns
    assert {
        "question_candidates",
        "candidate_image_parts",
        "screenshot_overlaps",
        "ordering_issues",
        "candidate_relationships",
        "review_actions",
        "wrong_book_entries",
        "practice_sessions",
        "practice_items",
        "attempts",
    } <= table_names
    assert {"mastered_at", "updated_at"} <= wrong_columns


def test_library_migration_backfills_existing_candidate_category(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "migration-backfill.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("QUESTION_PRACTICE_DATABASE_URL", database_url)
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    command.upgrade(config, "20260713_0005")

    with create_engine(database_url).begin() as connection:
        now = "2026-07-13 00:00:00"
        connection.execute(
            text("INSERT INTO courses (id, name, created_at, updated_at) VALUES ('course', '课程', :now, :now)"),
            {"now": now},
        )
        connection.execute(
            text("INSERT INTO categories (id, course_id, path, created_at) VALUES ('category', 'course', '分类', :now)"),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO import_batches "
                "(id, course_id, category_id, name, source_relative_path, status, uploaded_count, duplicate_count, created_at, updated_at) "
                "VALUES ('batch', 'course', 'category', '旧批次', 'input', 'ready', 1, 0, :now, :now)"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO source_images "
                "(id, batch_id, source_relative_path, original_name, safe_name, relative_path, sha256, mime_type, width, height, file_size, duplicate_of_id, order_index, created_at) "
                "VALUES ('image', 'batch', 'one.png', 'one.png', 'one.png', 'imports/one.png', :sha, 'image/png', 10, 10, 1, NULL, 0, :now)"
            ),
            {"sha": "0" * 64, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO question_candidates "
                "(id, batch_id, source_image_id, source_display_number, question_type, stem, options_json, correct_answer_json, source_correct_answer_raw, true_label, false_label, explanation, raw_ocr_text, normalized_ocr_text, ocr_confidence, parse_confidence, display_mode, anomaly_flags_json, status, created_at, updated_at) "
                "VALUES ('question', 'batch', 'image', '1', 'single_choice', '题干', :options, :answer, 'A', NULL, NULL, NULL, 'OCR', 'OCR', 1, 1, 'text', :flags, 'ready', :now, :now)"
            ),
            {"options": '[{"label":"A","text":"甲"}]', "answer": '"A"', "flags": "[]", "now": now},
        )

    command.upgrade(config, "head")

    with create_engine(database_url).connect() as connection:
        category_id = connection.execute(
            text("SELECT category_id FROM question_candidates WHERE id = 'question'")
        ).scalar_one()
    assert category_id == "category"


def test_upgrade_from_phase_seven_revision_to_head(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "phase-seven.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("QUESTION_PRACTICE_DATABASE_URL", database_url)
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    command.upgrade(config, "20260713_0006")

    command.upgrade(config, "head")

    with create_engine(database_url).connect() as connection:
        revision = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        wrong_columns = {
            column["name"]
            for column in inspect(connection).get_columns("wrong_book_entries")
        }
        tables = set(inspect(connection).get_table_names())
    assert revision == "20260807_0008"
    assert {"mastered_at", "updated_at"} <= wrong_columns
    assert {"practice_sessions", "practice_items", "attempts"} <= tables


def test_excluded_source_migration_reconciles_candidates_relationships_and_counts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "excluded-source-migration.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("QUESTION_PRACTICE_DATABASE_URL", database_url)
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    command.upgrade(config, "20260714_0007")
    engine = create_engine(database_url)

    with Session(engine) as session:
        course = Course(name="迁移课程")
        batch = ImportBatch(
            course=course,
            name="迁移批次",
            source_relative_path="迁移课程/迁移批次",
            status="ready",
            found_question_count=3,
            high_confidence_count=3,
            review_count=0,
        )
        excluded = SourceImage(
            batch=batch,
            source_relative_path="excluded.png",
            original_name="excluded.png",
            safe_name="excluded.png",
            relative_path="imports/excluded.png",
            sha256="1" * 64,
            mime_type="image/png",
            width=100,
            height=200,
            file_size=1,
            review_flag="unrelated",
            order_index=0,
            ocr_status="completed",
        )
        normal = SourceImage(
            batch=batch,
            source_relative_path="normal.png",
            original_name="normal.png",
            safe_name="normal.png",
            relative_path="imports/normal.png",
            sha256="2" * 64,
            mime_type="image/png",
            width=100,
            height=200,
            file_size=1,
            order_index=1,
            ocr_status="completed",
        )
        session.add_all([batch, excluded, normal])
        session.flush()

        def candidate(stem: str, source: SourceImage) -> QuestionCandidate:
            return QuestionCandidate(
                batch_id=batch.id,
                source_image=source,
                question_type="single_choice",
                stem=stem,
                options_json=[
                    {"label": "A", "text": "甲"},
                    {"label": "B", "text": "乙"},
                ],
                correct_answer_json="A",
                raw_ocr_text=stem,
                normalized_ocr_text=stem,
                ocr_confidence=1,
                parse_confidence=1,
                display_mode="text_first",
                anomaly_flags_json=[],
                status="ready",
            )

        ignored = candidate("应忽略", excluded)
        kept = candidate("应保留", normal)
        mixed = candidate("应复核", normal)
        session.add_all([ignored, kept, mixed])
        session.flush()
        session.add_all(
            [
                CandidateImagePart(
                    candidate_id=mixed.id,
                    source_image_id=normal.id,
                    crop_path="crops/mixed-0.png",
                    source_y_start=0,
                    source_y_end=50,
                    part_index=0,
                ),
                CandidateImagePart(
                    candidate_id=mixed.id,
                    source_image_id=excluded.id,
                    crop_path="crops/mixed-1.png",
                    source_y_start=50,
                    source_y_end=100,
                    part_index=1,
                ),
            ]
        )
        relationship = CandidateRelationship(
            batch_id=batch.id,
            primary_candidate_id=ignored.id,
            secondary_candidate_id=kept.id,
            relation_type="cross_screenshot",
            decision="suggested",
            confidence=0.9,
            evidence_json={},
            status="pending",
        )
        session.add(relationship)
        session.commit()
        ids = batch.id, ignored.id, kept.id, mixed.id, relationship.id

    command.upgrade(config, "head")

    with Session(engine) as session:
        batch_id, ignored_id, kept_id, mixed_id, relationship_id = ids
        batch = session.get(ImportBatch, batch_id)
        ignored = session.get(QuestionCandidate, ignored_id)
        kept = session.get(QuestionCandidate, kept_id)
        mixed = session.get(QuestionCandidate, mixed_id)
        relationship = session.get(CandidateRelationship, relationship_id)
        assert ignored.status == "ignored"
        assert kept.status == "ready"
        assert mixed.status == "review"
        assert [flag["code"] for flag in mixed.anomaly_flags_json] == [
            "EXCLUDED_SOURCE_IMAGE"
        ]
        assert (relationship.status, relationship.decision) == (
            "resolved",
            "kept_secondary",
        )
        assert (
            batch.found_question_count,
            batch.high_confidence_count,
            batch.review_count,
        ) == (2, 1, 1)
        assert batch.status == "question_review"

    with engine.connect() as connection:
        revision = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
    assert revision == "20260807_0008"

    command.downgrade(config, "20260714_0007")
    command.upgrade(config, "head")
    with Session(engine) as session:
        mixed = session.get(QuestionCandidate, ids[3])
        assert [flag["code"] for flag in mixed.anomaly_flags_json].count(
            "EXCLUDED_SOURCE_IMAGE"
        ) == 1
    engine.dispose()


def test_excluded_source_migration_reconciles_cross_batch_primary_and_part_sources(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "cross-batch-excluded-source-migration.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("QUESTION_PRACTICE_DATABASE_URL", database_url)
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    command.upgrade(config, "20260714_0007")
    engine = create_engine(database_url)

    with Session(engine) as session:
        course = Course(name="跨批次迁移课程")
        source_batch = ImportBatch(
            course=course,
            name="排除来源批次",
            source_relative_path="跨批次迁移课程/排除来源批次",
            status="ready",
        )
        primary_batch = ImportBatch(
            course=course,
            name="跨批次主来源批次",
            source_relative_path="跨批次迁移课程/跨批次主来源批次",
            status="ready",
            found_question_count=2,
            high_confidence_count=2,
            review_count=0,
        )
        part_batch = ImportBatch(
            course=course,
            name="跨批次片段批次",
            source_relative_path="跨批次迁移课程/跨批次片段批次",
            status="ready",
            found_question_count=1,
            high_confidence_count=1,
            review_count=0,
        )
        excluded = SourceImage(
            batch=source_batch,
            source_relative_path="excluded.png",
            original_name="excluded.png",
            safe_name="excluded.png",
            relative_path="imports/excluded.png",
            sha256="3" * 64,
            mime_type="image/png",
            width=100,
            height=200,
            file_size=1,
            review_flag="damaged",
            order_index=0,
            ocr_status="completed",
        )
        primary_normal = SourceImage(
            batch=primary_batch,
            source_relative_path="primary-normal.png",
            original_name="primary-normal.png",
            safe_name="primary-normal.png",
            relative_path="imports/primary-normal.png",
            sha256="4" * 64,
            mime_type="image/png",
            width=100,
            height=200,
            file_size=1,
            order_index=0,
            ocr_status="completed",
        )
        part_normal = SourceImage(
            batch=part_batch,
            source_relative_path="part-normal.png",
            original_name="part-normal.png",
            safe_name="part-normal.png",
            relative_path="imports/part-normal.png",
            sha256="5" * 64,
            mime_type="image/png",
            width=100,
            height=200,
            file_size=1,
            order_index=0,
            ocr_status="completed",
        )
        session.add_all([excluded, primary_normal, part_normal])
        session.flush()

        def candidate(
            stem: str,
            batch: ImportBatch,
            source: SourceImage,
        ) -> QuestionCandidate:
            return QuestionCandidate(
                batch_id=batch.id,
                source_image=source,
                question_type="single_choice",
                stem=stem,
                options_json=[
                    {"label": "A", "text": "甲"},
                    {"label": "B", "text": "乙"},
                ],
                correct_answer_json="A",
                raw_ocr_text=stem,
                normalized_ocr_text=stem,
                ocr_confidence=1,
                parse_confidence=1,
                display_mode="text_first",
                anomaly_flags_json=[],
                status="ready",
            )

        cross_primary = candidate("跨批次主来源", primary_batch, excluded)
        kept_primary = candidate("主来源保留题", primary_batch, primary_normal)
        cross_part = candidate("跨批次片段来源", part_batch, part_normal)
        session.add_all([cross_primary, kept_primary, cross_part])
        session.flush()
        session.add(
            CandidateImagePart(
                candidate_id=cross_part.id,
                source_image_id=excluded.id,
                crop_path="crops/cross-batch-part.png",
                source_y_start=0,
                source_y_end=100,
                part_index=0,
            )
        )
        relationship = CandidateRelationship(
            batch_id=primary_batch.id,
            primary_candidate_id=cross_primary.id,
            secondary_candidate_id=kept_primary.id,
            relation_type="cross_screenshot",
            decision="suggested",
            confidence=0.9,
            evidence_json={},
            status="pending",
        )
        session.add(relationship)
        session.commit()
        ids = (
            primary_batch.id,
            part_batch.id,
            cross_primary.id,
            kept_primary.id,
            cross_part.id,
            relationship.id,
        )

    unbounded_id_queries: list[str] = []

    def capture_migration_query(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        normalized = " ".join(statement.split())
        dynamically_expanded_id_filters = (
            "question_candidates.source_image_id IN (",
            "candidate_image_parts.source_image_id IN (",
            "candidate_image_parts.candidate_id IN (",
        )
        if any(
            marker in normalized for marker in dynamically_expanded_id_filters
        ):
            unbounded_id_queries.append(normalized)

    event.listen(Engine, "before_cursor_execute", capture_migration_query)
    try:
        command.upgrade(config, "head")
    finally:
        event.remove(Engine, "before_cursor_execute", capture_migration_query)

    assert unbounded_id_queries == []

    with Session(engine) as session:
        (
            primary_batch_id,
            part_batch_id,
            cross_primary_id,
            kept_primary_id,
            cross_part_id,
            relationship_id,
        ) = ids
        primary_batch = session.get(ImportBatch, primary_batch_id)
        part_batch = session.get(ImportBatch, part_batch_id)
        cross_primary = session.get(QuestionCandidate, cross_primary_id)
        kept_primary = session.get(QuestionCandidate, kept_primary_id)
        cross_part = session.get(QuestionCandidate, cross_part_id)
        relationship = session.get(CandidateRelationship, relationship_id)
        assert cross_primary.status == "ignored"
        assert kept_primary.status == "ready"
        assert (relationship.status, relationship.decision) == (
            "resolved",
            "kept_secondary",
        )
        assert (
            primary_batch.found_question_count,
            primary_batch.high_confidence_count,
            primary_batch.review_count,
            primary_batch.status,
        ) == (1, 1, 0, "ready")
        assert cross_part.status == "review"
        assert [flag["code"] for flag in cross_part.anomaly_flags_json] == [
            "EXCLUDED_SOURCE_IMAGE"
        ]
        assert (
            part_batch.found_question_count,
            part_batch.high_confidence_count,
            part_batch.review_count,
            part_batch.status,
        ) == (1, 0, 1, "question_review")
    engine.dispose()


def test_excluded_source_migration_globally_resolves_cross_batch_relationships(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "global-relationship-excluded-source-migration.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("QUESTION_PRACTICE_DATABASE_URL", database_url)
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    command.upgrade(config, "20260714_0007")
    engine = create_engine(database_url)

    with Session(engine) as session:
        course = Course(name="迁移全局关系课程")
        source_batch = ImportBatch(
            course=course,
            name="迁移排除来源批次",
            source_relative_path="迁移全局关系课程/迁移排除来源批次",
            status="ready",
        )
        primary_batch = ImportBatch(
            course=course,
            name="迁移关系主端批次",
            source_relative_path="迁移全局关系课程/迁移关系主端批次",
            status="question_review",
            found_question_count=1,
            high_confidence_count=1,
        )
        secondary_batch = ImportBatch(
            course=course,
            name="迁移关系次端批次",
            source_relative_path="迁移全局关系课程/迁移关系次端批次",
            status="ready",
            found_question_count=1,
            high_confidence_count=1,
        )
        owner_batch = ImportBatch(
            course=course,
            name="迁移关系独立归属批次",
            source_relative_path="迁移全局关系课程/迁移关系独立归属批次",
            status="question_review",
        )
        excluded = SourceImage(
            batch=source_batch,
            source_relative_path="excluded-global.png",
            original_name="excluded-global.png",
            safe_name="excluded-global.png",
            relative_path="imports/excluded-global.png",
            sha256="6" * 64,
            mime_type="image/png",
            width=100,
            height=200,
            file_size=1,
            review_flag="unrelated",
            order_index=0,
            ocr_status="completed",
        )
        session.add(excluded)
        session.flush()

        def candidate(stem: str, batch: ImportBatch) -> QuestionCandidate:
            return QuestionCandidate(
                batch_id=batch.id,
                source_image=excluded,
                question_type="single_choice",
                stem=stem,
                options_json=[
                    {"label": "A", "text": "甲"},
                    {"label": "B", "text": "乙"},
                ],
                correct_answer_json="A",
                raw_ocr_text=stem,
                normalized_ocr_text=stem,
                ocr_confidence=1,
                parse_confidence=1,
                display_mode="text_first",
                anomaly_flags_json=[],
                status="ready",
            )

        primary = candidate("迁移跨批次主端", primary_batch)
        secondary = candidate("迁移跨批次次端", secondary_batch)
        session.add_all([primary, secondary])
        session.flush()
        primary_owned = CandidateRelationship(
            batch_id=primary_batch.id,
            primary_candidate_id=primary.id,
            secondary_candidate_id=secondary.id,
            relation_type="cross_screenshot",
            decision="suggested",
            confidence=0.9,
            evidence_json={},
            status="pending",
        )
        third_party_owned = CandidateRelationship(
            batch_id=owner_batch.id,
            primary_candidate_id=primary.id,
            secondary_candidate_id=secondary.id,
            relation_type="possible_duplicate",
            decision="suggested",
            confidence=0.8,
            evidence_json={},
            status="pending",
        )
        session.add_all([primary_owned, third_party_owned])
        session.commit()
        ids = {
            "source_batch": source_batch.id,
            "primary_batch": primary_batch.id,
            "secondary_batch": secondary_batch.id,
            "owner_batch": owner_batch.id,
            "primary": primary.id,
            "secondary": secondary.id,
            "relationships": (primary_owned.id, third_party_owned.id),
        }

    command.upgrade(config, "head")

    with Session(engine) as session:
        assert session.get(QuestionCandidate, ids["primary"]).status == "ignored"
        assert session.get(QuestionCandidate, ids["secondary"]).status == "ignored"
        for relationship_id in ids["relationships"]:
            relationship = session.get(CandidateRelationship, relationship_id)
            assert relationship is not None
            assert (relationship.status, relationship.decision) == (
                "resolved",
                "source_images_excluded",
            )
        for key in (
            "source_batch",
            "primary_batch",
            "secondary_batch",
            "owner_batch",
        ):
            batch = session.get(ImportBatch, ids[key])
            assert batch is not None
            assert (
                batch.found_question_count,
                batch.high_confidence_count,
                batch.review_count,
                batch.status,
            ) == (0, 0, 0, "ready")
    engine.dispose()
