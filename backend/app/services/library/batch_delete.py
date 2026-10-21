from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    CandidateImagePart,
    CandidateRelationship,
    ImportBatch,
    OrderingIssue,
    PracticeItem,
    QuestionCandidate,
    ReviewAction,
    ScreenshotOverlap,
    SourceImage,
    WrongBookEntry,
)
from app.services.library.deletion_recovery import (
    BatchDeletionJournal,
    BatchDeletionOperation,
    BatchDeletionRecoveryError,
)
from app.services.practice.image_lifecycle import practice_image_lifecycle
from app.services.practice.snapshot_files import (
    PracticeSnapshotReferenceError,
    collect_practice_snapshot_crop_paths,
)


logger = logging.getLogger(__name__)


class LibraryDeletionError(ValueError):
    pass


@dataclass(frozen=True)
class DeletedBatch:
    id: str
    name: str
    deleted_questions: int
    deleted_images: int
    cleanup_pending: bool


def _safe_relative_paths(root: Path, relative_paths: set[str]) -> set[str]:
    resolved_root = root.resolve()
    result: set[str] = set()
    for relative_path in sorted(relative_paths):
        source = (resolved_root / Path(relative_path)).resolve()
        if resolved_root not in source.parents:
            raise LibraryDeletionError("应用内部文件路径无效，已取消删除")
        if source.is_file():
            result.add(Path(relative_path).as_posix())
    return result


def _restore_after_failure(
    journal: BatchDeletionJournal,
    operation: BatchDeletionOperation,
) -> None:
    try:
        journal.restore(operation)
    except BatchDeletionRecoveryError:
        logger.critical(
            "批次删除失败后的文件恢复不完整，将在下次启动继续，操作编号：%s",
            operation.operation_id,
            exc_info=True,
        )
        return
    if not journal.cleanup(operation):
        logger.warning(
            "批次删除回滚完成，但暂存目录清理待重试，操作编号：%s",
            operation.operation_id,
        )


def delete_import_batch(
    session: Session,
    settings: Settings,
    batch_id: str,
) -> DeletedBatch:
    with practice_image_lifecycle():
        return _delete_import_batch(session, settings, batch_id)


def _delete_import_batch(
    session: Session,
    settings: Settings,
    batch_id: str,
) -> DeletedBatch:
    batch = session.get(ImportBatch, batch_id)
    if batch is None:
        raise LibraryDeletionError("导入批次不存在")
    candidate_ids = list(
        session.scalars(
            select(QuestionCandidate.id).where(QuestionCandidate.batch_id == batch_id)
        )
    )
    source_ids = list(
        session.scalars(select(SourceImage.id).where(SourceImage.batch_id == batch_id))
    )
    crop_paths = set(
        session.scalars(
            select(CandidateImagePart.crop_path)
            .join(QuestionCandidate, QuestionCandidate.id == CandidateImagePart.candidate_id)
            .where(QuestionCandidate.batch_id == batch_id)
        )
    )
    outside_crop_paths = set(
        session.scalars(
            select(CandidateImagePart.crop_path)
            .join(QuestionCandidate, QuestionCandidate.id == CandidateImagePart.candidate_id)
            .where(QuestionCandidate.batch_id != batch_id)
        )
    )
    source_paths = set(
        session.scalars(
            select(SourceImage.relative_path).where(SourceImage.batch_id == batch_id)
        )
    )
    outside_source_paths = set(
        session.scalars(
            select(SourceImage.relative_path).where(SourceImage.batch_id != batch_id)
        )
    )
    try:
        practice_crop_paths = collect_practice_snapshot_crop_paths(
            session.scalars(select(PracticeItem.image_parts_snapshot_json))
        )
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        TypeError,
        PracticeSnapshotReferenceError,
    ) as error:
        raise LibraryDeletionError("练习图片快照无效，已取消删除") from error
    relative_paths = _safe_relative_paths(
        settings.data_dir,
        (crop_paths - outside_crop_paths - practice_crop_paths)
        | (source_paths - outside_source_paths),
    )
    journal = BatchDeletionJournal(settings.data_dir)
    operation = journal.prepare(batch_id, relative_paths)
    try:
        journal.stage(operation)
    except BaseException:
        _restore_after_failure(journal, operation)
        raise
    result = DeletedBatch(
        batch.id,
        batch.name,
        len(candidate_ids),
        len(source_ids),
        cleanup_pending=False,
    )
    try:
        if source_ids:
            session.execute(
                update(SourceImage)
                .where(
                    SourceImage.batch_id != batch_id,
                    SourceImage.duplicate_of_id.in_(source_ids),
                )
                .values(duplicate_of_id=None)
            )
        if candidate_ids:
            session.execute(
                update(PracticeItem)
                .where(PracticeItem.question_id.in_(candidate_ids))
                .values(question_id=None)
            )
            session.execute(
                update(QuestionCandidate)
                .where(
                    QuestionCandidate.batch_id != batch_id,
                    QuestionCandidate.duplicate_of_id.in_(candidate_ids),
                )
                .values(duplicate_of_id=None)
            )
            session.execute(
                delete(WrongBookEntry).where(
                    WrongBookEntry.question_id.in_(candidate_ids)
                )
            )
        session.execute(delete(ReviewAction).where(ReviewAction.batch_id == batch_id))
        session.execute(
            delete(CandidateRelationship).where(
                CandidateRelationship.batch_id == batch_id
            )
        )
        if candidate_ids:
            session.execute(
                delete(CandidateImagePart).where(
                    CandidateImagePart.candidate_id.in_(candidate_ids)
                )
            )
            session.execute(
                delete(QuestionCandidate).where(
                    QuestionCandidate.id.in_(candidate_ids)
                )
            )
        session.execute(
            delete(ScreenshotOverlap).where(ScreenshotOverlap.batch_id == batch_id)
        )
        session.execute(delete(OrderingIssue).where(OrderingIssue.batch_id == batch_id))
        session.execute(delete(SourceImage).where(SourceImage.batch_id == batch_id))
        session.execute(delete(ImportBatch).where(ImportBatch.id == batch_id))
        session.commit()
    except BaseException:
        try:
            session.rollback()
        except BaseException:
            logger.critical(
                "批次删除失败后的数据库回滚未完成，操作编号：%s",
                operation.operation_id,
                exc_info=True,
            )
        _restore_after_failure(journal, operation)
        raise
    cleanup_pending = not journal.cleanup(operation)
    if cleanup_pending:
        logger.warning(
            "批次已从数据库删除，但内部文件暂存清理待重试，操作编号：%s",
            operation.operation_id,
        )
    return DeletedBatch(
        result.id,
        result.name,
        result.deleted_questions,
        result.deleted_images,
        cleanup_pending=cleanup_pending,
    )
