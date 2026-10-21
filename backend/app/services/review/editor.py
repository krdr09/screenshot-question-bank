from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings
from app.cropping.questions import CropBounds
from app.models import (
    CandidateImagePart,
    CandidateRelationship,
    Category,
    ImportBatch,
    OrderingIssue,
    QuestionCandidate,
    ReviewAction,
    SourceImage,
    WrongBookEntry,
)
from app.services.review.crop_files import CropFileTransaction, CropReplacement
from app.services.review.snapshots import capture_candidate, mark_undone, record_action, restore_candidate


logger = logging.getLogger(__name__)


class ReviewValidationError(ValueError):
    pass


INACTIVE_CANDIDATE_STATUSES = {
    "deleted",
    "ignored",
    "duplicate_merged",
    "merged_source",
    "replaced_by_ocr",
}


def _candidate(session: Session, candidate_id: str) -> QuestionCandidate:
    candidate = session.scalar(
        select(QuestionCandidate)
        .options(selectinload(QuestionCandidate.image_parts))
        .where(QuestionCandidate.id == candidate_id)
    )
    if candidate is None:
        raise ReviewValidationError("候选题不存在")
    return candidate


def _validate_structure(data: dict) -> None:
    question_type = data["question_type"]
    if question_type not in {"single_choice", "true_false"}:
        raise ReviewValidationError("仅支持单选题和判断题")
    options = data["options_json"]
    if len(options) < 2:
        raise ReviewValidationError("选项至少需要两个")
    labels = [str(item.get("label", "")).strip().upper() for item in options]
    if labels != [chr(ord("A") + index) for index in range(len(labels))]:
        raise ReviewValidationError("选项字母必须从 A 开始连续且不重复")
    if any(not str(item.get("text", "")).strip() for item in options):
        raise ReviewValidationError("选项文本不能为空")
    answer = data["correct_answer_json"]
    if question_type == "single_choice":
        if answer not in labels:
            raise ReviewValidationError("单选题正确答案必须属于现有选项")
    else:
        true_label = data["true_label"]
        false_label = data["false_label"]
        if not isinstance(answer, bool) or true_label not in labels or false_label not in labels or true_label == false_label:
            raise ReviewValidationError("判断题必须设置不同的正确/错误选项映射和布尔答案")
    if data["display_mode"] not in {"text", "image_preferred", "text_first", "image_first"}:
        raise ReviewValidationError("显示模式无效")
    if not str(data["stem"]).strip():
        raise ReviewValidationError("题干不能为空")


def _prospective(candidate: QuestionCandidate, changes: dict) -> dict:
    aliases = {
        "options": "options_json",
        "correct_answer": "correct_answer_json",
    }
    values = {
        "question_type": candidate.question_type,
        "stem": candidate.stem,
        "options_json": candidate.options_json,
        "correct_answer_json": candidate.correct_answer_json,
        "true_label": candidate.true_label,
        "false_label": candidate.false_label,
        "display_mode": candidate.display_mode,
    }
    for key, value in changes.items():
        field = aliases.get(key, key)
        if field in values:
            values[field] = value
    return values


@dataclass(frozen=True)
class _RecropPlan:
    part: CandidateImagePart
    crop_path: str
    y_start: int
    y_end: int
    replacement: CropReplacement


def _validate_and_plan_recrops(
    session: Session,
    settings: Settings,
    candidate: QuestionCandidate,
    changes: list[dict],
) -> list[_RecropPlan]:
    by_id = {part.id: part for part in candidate.image_parts}
    by_index = {part.part_index: part for part in candidate.image_parts}
    planned: list[_RecropPlan] = []
    seen_part_ids: set[str] = set()
    for requested in changes:
        part = by_id.get(requested.get("id")) or by_index.get(requested.get("part_index"))
        if part is None:
            raise ReviewValidationError("裁切片段不存在")
        if part.id in seen_part_ids:
            raise ReviewValidationError("同一裁切片段不能重复提交")
        seen_part_ids.add(part.id)
        image = session.get(SourceImage, part.source_image_id)
        if image is None:
            raise ReviewValidationError("来源图片不存在")
        y_start = int(requested["source_y_start"])
        y_end = int(requested["source_y_end"])
        if y_start < 0 or y_end > image.height or y_start >= y_end:
            raise ReviewValidationError("裁切范围必须位于原图内")
        root = settings.data_dir.resolve()
        source = (settings.data_dir / Path(image.relative_path)).resolve()
        shared_crop = session.scalar(
            select(CandidateImagePart.id)
            .where(
                CandidateImagePart.crop_path == part.crop_path,
                CandidateImagePart.id != part.id,
            )
            .limit(1)
        )
        crop_path = part.crop_path
        if shared_crop:
            crop_path = (
                Path("crops")
                / candidate.batch_id
                / f"{candidate.id}-part-{part.part_index}.png"
            ).as_posix()
        target = (settings.data_dir / Path(crop_path)).resolve()
        if root not in source.parents or root not in target.parents or not source.is_file():
            raise ReviewValidationError("裁切图片路径无效")
        planned.append(
            _RecropPlan(
                part=part,
                crop_path=crop_path,
                y_start=y_start,
                y_end=y_end,
                replacement=CropReplacement(
                    source=source,
                    target=target,
                    bounds=CropBounds(
                        y_start=y_start,
                        y_end=y_end,
                        page_height=image.height,
                    ),
                ),
            )
        )
    return planned


def update_candidate(
    session: Session,
    settings: Settings,
    candidate_id: str,
    changes: dict,
) -> QuestionCandidate:
    candidate = _candidate(session, candidate_id)
    if "category_id" in changes:
        batch = session.get(ImportBatch, candidate.batch_id)
        category_id = changes["category_id"]
        category = session.get(Category, category_id) if category_id else None
        if batch is None or (category_id and (category is None or category.course_id != batch.course_id)):
            raise ReviewValidationError("分类必须属于题目所在课程")
    values = _prospective(candidate, changes)
    _validate_structure(values)
    before = capture_candidate(candidate)
    recrop_plans = (
        _validate_and_plan_recrops(session, settings, candidate, changes["parts"])
        if "parts" in changes
        else []
    )
    file_transaction = CropFileTransaction.stage(
        [plan.replacement for plan in recrop_plans]
    )
    aliases = {"options": "options_json", "correct_answer": "correct_answer_json"}
    editable = {
        "edited_ocr_text", "stem", "question_type", "options_json",
        "correct_answer_json", "true_label", "false_label", "explanation",
        "display_mode", "source_display_number", "category_id",
    }
    try:
        for key, value in changes.items():
            field = aliases.get(key, key)
            if field in editable:
                setattr(candidate, field, value)
        for plan in recrop_plans:
            plan.part.crop_path = plan.crop_path
            plan.part.source_y_start = plan.y_start
            plan.part.source_y_end = plan.y_end
        session.flush()
        record_action(session, candidate, "update", before, capture_candidate(candidate))
        file_transaction.install()
        session.commit()
    except Exception:
        session.rollback()
        file_transaction.rollback()
        raise
    else:
        file_transaction.finalize()
    return _candidate(session, candidate.id)


def update_relationship_crops(
    session: Session,
    settings: Settings,
    relationship_id: str,
    primary_parts: list[dict],
    secondary_parts: list[dict],
) -> tuple[QuestionCandidate, QuestionCandidate]:
    relationship = session.get(CandidateRelationship, relationship_id)
    if relationship is None:
        raise ReviewValidationError("候选关系不存在")
    if not primary_parts or not secondary_parts:
        raise ReviewValidationError("两侧裁切范围都不能为空")
    primary = _candidate(session, relationship.primary_candidate_id)
    secondary = _candidate(session, relationship.secondary_candidate_id)
    if (
        primary.batch_id != relationship.batch_id
        or secondary.batch_id != relationship.batch_id
    ):
        raise ReviewValidationError("候选关系与题目批次不一致")

    primary_before = capture_candidate(primary)
    secondary_before = capture_candidate(secondary)
    primary_plans = _validate_and_plan_recrops(
        session,
        settings,
        primary,
        primary_parts,
    )
    secondary_plans = _validate_and_plan_recrops(
        session,
        settings,
        secondary,
        secondary_parts,
    )
    plans = [*primary_plans, *secondary_plans]
    file_transaction = CropFileTransaction.stage(
        [plan.replacement for plan in plans]
    )
    try:
        for plan in plans:
            plan.part.crop_path = plan.crop_path
            plan.part.source_y_start = plan.y_start
            plan.part.source_y_end = plan.y_end
        session.flush()
        record_action(
            session,
            primary,
            "update",
            primary_before,
            capture_candidate(primary),
        )
        record_action(
            session,
            secondary,
            "update",
            secondary_before,
            capture_candidate(secondary),
        )
        file_transaction.install()
        session.commit()
    except Exception:
        session.rollback()
        file_transaction.rollback()
        raise
    else:
        file_transaction.finalize()
    return _candidate(session, primary.id), _candidate(session, secondary.id)


def has_pending_relationship(session: Session, candidate_id: str) -> bool:
    relationship_id = session.scalar(
        select(CandidateRelationship.id)
        .where(
            CandidateRelationship.status == "pending",
            or_(
                CandidateRelationship.primary_candidate_id == candidate_id,
                CandidateRelationship.secondary_candidate_id == candidate_id,
            ),
        )
        .limit(1)
    )
    return relationship_id is not None


def update_batch_review_state(session: Session, batch_id: str) -> ImportBatch:
    batch = session.get(ImportBatch, batch_id)
    if batch is None:
        raise ReviewValidationError("导入批次不存在")
    candidates = list(session.scalars(select(QuestionCandidate).where(QuestionCandidate.batch_id == batch_id)))
    pending_relationships = list(
        session.execute(
            select(
                CandidateRelationship.primary_candidate_id,
                CandidateRelationship.secondary_candidate_id,
            ).where(
                CandidateRelationship.batch_id == batch_id,
                CandidateRelationship.status == "pending",
            )
        )
    )
    pending_candidate_ids = {
        candidate_id
        for relationship in pending_relationships
        for candidate_id in relationship
    }
    for candidate in candidates:
        if candidate.id in pending_candidate_ids and candidate.status == "ready":
            candidate.status = "review"

    batch.found_question_count = sum(item.status not in INACTIVE_CANDIDATE_STATUSES for item in candidates)
    batch.high_confidence_count = sum(item.status == "ready" for item in candidates)
    batch.review_count = sum(item.status == "review" for item in candidates)
    ordering_pending = session.scalar(
        select(OrderingIssue.id).where(OrderingIssue.batch_id == batch_id, OrderingIssue.status == "pending").limit(1)
    )
    if ordering_pending:
        batch.status = "ordering_review"
    elif batch.review_count or pending_relationships:
        batch.status = "question_review"
    elif batch.error_count:
        batch.status = "partially_ready"
    else:
        batch.status = "ready"
    session.flush()
    return batch


def _set_status(session: Session, candidate_id: str, status: str, action_type: str) -> QuestionCandidate:
    candidate = _candidate(session, candidate_id)
    if status in {"ready", "ignored", "deleted"} and has_pending_relationship(session, candidate_id):
        raise ReviewValidationError("请先处理与该题关联的待处理候选关系")
    if status == "ready":
        _validate_structure(_prospective(candidate, {}))
    before = capture_candidate(candidate)
    candidate.status = status
    if status == "deleted":
        session.execute(
            delete(WrongBookEntry).where(WrongBookEntry.question_id == candidate.id)
        )
    session.flush()
    record_action(session, candidate, action_type, before, capture_candidate(candidate))
    update_batch_review_state(session, candidate.batch_id)
    session.commit()
    return _candidate(session, candidate.id)


def confirm_candidate(session: Session, candidate_id: str) -> QuestionCandidate:
    return _set_status(session, candidate_id, "ready", "confirm")


def ignore_candidate(session: Session, candidate_id: str) -> QuestionCandidate:
    return _set_status(session, candidate_id, "ignored", "ignore")


def delete_candidate(session: Session, candidate_id: str) -> QuestionCandidate:
    return _set_status(session, candidate_id, "deleted", "delete")


def _undo_crop_replacements(
    session: Session,
    settings: Settings,
    candidate: QuestionCandidate,
) -> list[CropReplacement]:
    replacements: list[CropReplacement] = []
    root = settings.data_dir.resolve()
    for part in candidate.image_parts:
        image = session.get(SourceImage, part.source_image_id)
        if image is None:
            continue
        source = (settings.data_dir / Path(image.relative_path)).resolve()
        target = (settings.data_dir / Path(part.crop_path)).resolve()
        if root in source.parents and root in target.parents and source.is_file():
            replacements.append(
                CropReplacement(
                    source=source,
                    target=target,
                    bounds=CropBounds(
                        y_start=part.source_y_start,
                        y_end=part.source_y_end,
                        page_height=image.height,
                    ),
                )
            )
    return replacements


def undo_last_action(session: Session, settings: Settings, candidate_id: str) -> QuestionCandidate:
    candidate = _candidate(session, candidate_id)
    action = session.scalar(
        select(ReviewAction)
        .where(ReviewAction.candidate_id == candidate_id, ReviewAction.undone_at.is_(None))
        .order_by(ReviewAction.created_at.desc(), ReviewAction.id.desc())
        .limit(1)
    )
    if action is None:
        raise ReviewValidationError("没有可撤销的修改")
    file_transaction: CropFileTransaction | None = None
    try:
        if action.action_type == "merge":
            restore_candidate(session, candidate, action.before_json["primary"])
            secondary = _candidate(session, action.before_json["relationship"]["secondary_candidate_id"])
            restore_candidate(session, secondary, action.before_json["secondary"])
            relationship = session.get(CandidateRelationship, action.before_json["relationship"]["id"])
            if relationship and action.before_json.get("relationship_created"):
                session.delete(relationship)
            elif relationship:
                relationship.primary_candidate_id = action.before_json["relationship"]["primary_candidate_id"]
                relationship.secondary_candidate_id = action.before_json["relationship"]["secondary_candidate_id"]
                relationship.decision = action.before_json["relationship"]["decision"]
                relationship.status = action.before_json["relationship"]["status"]
        elif action.action_type == "split":
            restore_candidate(session, candidate, action.before_json["primary"])
            created = session.get(QuestionCandidate, action.before_json["created_candidate_id"])
            if created:
                created.status = "deleted"
        elif action.action_type == "reorder":
            for item in action.before_json["orders"]:
                image = session.get(SourceImage, item["id"])
                if image:
                    image.order_index = item["order_index"]
        elif action.action_type == "relationship":
            relationship_data = action.before_json["relationship"]
            restore_candidate(session, candidate, action.before_json["primary"])
            secondary = _candidate(session, relationship_data["secondary_candidate_id"])
            restore_candidate(session, secondary, action.before_json["secondary"])
            relationship = session.get(CandidateRelationship, relationship_data["id"])
            if relationship:
                relationship.primary_candidate_id = relationship_data["primary_candidate_id"]
                relationship.secondary_candidate_id = relationship_data["secondary_candidate_id"]
                relationship.decision = relationship_data["decision"]
                relationship.status = relationship_data["status"]
        elif action.action_type == "duplicate":
            restore_candidate(session, candidate, action.before_json["candidate"])
            relationship = session.get(CandidateRelationship, action.before_json["relationship_id"])
            if relationship and not action.before_json["relationship_existed"]:
                session.delete(relationship)
            elif relationship and action.before_json["relationship"]:
                relationship_data = action.before_json["relationship"]
                relationship.primary_candidate_id = relationship_data["primary_candidate_id"]
                relationship.secondary_candidate_id = relationship_data["secondary_candidate_id"]
                relationship.decision = relationship_data["decision"]
                relationship.status = relationship_data["status"]
        elif action.action_type == "rerun_source_ocr":
            for restored_id, snapshot in action.before_json["candidates"].items():
                restored = _candidate(session, restored_id)
                restore_candidate(session, restored, snapshot)
            for created_id in action.before_json["created_candidate_ids"]:
                created = session.get(QuestionCandidate, created_id)
                if created:
                    created.status = "deleted"
            image_data = action.before_json["image"]
            image = session.get(SourceImage, image_data["id"])
            if image:
                image.ocr_status = image_data["ocr_status"]
                image.ocr_result_json = image_data["ocr_result_json"]
                image.ocr_text = image_data["ocr_text"]
                image.ocr_error = image_data["ocr_error"]
        else:
            restore_candidate(session, candidate, action.before_json)
        file_transaction = CropFileTransaction.stage(
            _undo_crop_replacements(session, settings, candidate)
        )
        mark_undone(action)
        update_batch_review_state(session, candidate.batch_id)
        file_transaction.install()
        session.commit()
    except Exception:
        try:
            session.rollback()
        except Exception:
            logger.warning(
                "撤销失败后的数据库回滚失败",
                exc_info=True,
            )
        if file_transaction is not None:
            try:
                file_transaction.rollback()
            except Exception:
                logger.warning(
                    "撤销失败后的裁切文件回滚失败",
                    exc_info=True,
                )
        raise
    else:
        try:
            file_transaction.finalize()
        except Exception:
            logger.warning(
                "撤销已提交，但裁切事务清理失败",
                exc_info=True,
            )
    return _candidate(session, candidate.id)
