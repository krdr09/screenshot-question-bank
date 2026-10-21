from __future__ import annotations

import re
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session, selectinload

from app.config import Settings
from app.cropping.questions import CropBounds, crop_question_image
from app.models import CandidateImagePart, CandidateRelationship, QuestionCandidate, ReviewAction, SourceImage
from app.ocr.base import OcrEngine, OcrPageResult
from app.parsing.normalization import normalize_ocr_text
from app.parsing.questions import ParsedQuestion, parse_ocr_page, parse_ocr_text
from app.services.review.crop_files import CropFileTransaction, CropReplacement
from app.services.review.editor import (
    INACTIVE_CANDIDATE_STATUSES,
    ReviewValidationError,
    has_pending_relationship,
    update_batch_review_state,
)
from app.services.review.relationship_state import (
    ReviewRelationshipConflictError,
    claim_pending_relationship,
)
from app.services.review.snapshots import capture_candidate, record_action


def _candidate(session: Session, candidate_id: str) -> QuestionCandidate:
    candidate = session.scalar(
        select(QuestionCandidate)
        .options(selectinload(QuestionCandidate.image_parts))
        .where(QuestionCandidate.id == candidate_id)
        .execution_options(populate_existing=True)
    )
    if candidate is None:
        raise ReviewValidationError("候选题不存在")
    return candidate


def _require_active_candidates(*candidates: QuestionCandidate) -> None:
    if any(
        candidate.status in INACTIVE_CANDIDATE_STATUSES
        for candidate in candidates
    ):
        raise ReviewValidationError("只能操作有效候选题")


def _active_candidates(
    session: Session,
    primary_id: str,
    secondary_id: str,
) -> tuple[QuestionCandidate, QuestionCandidate]:
    primary = _candidate(session, primary_id)
    secondary = _candidate(session, secondary_id)
    _require_active_candidates(primary, secondary)
    return primary, secondary


def _relationship_snapshot(item: CandidateRelationship) -> dict:
    return {
        "id": item.id,
        "primary_candidate_id": item.primary_candidate_id,
        "secondary_candidate_id": item.secondary_candidate_id,
        "decision": item.decision,
        "status": item.status,
    }


def _apply_parsed(candidate: QuestionCandidate, question: ParsedQuestion) -> None:
    candidate.source_display_number = question.source_display_number
    candidate.question_type = question.question_type
    candidate.stem = question.stem
    candidate.options_json = [option.model_dump(mode="json") for option in question.options]
    candidate.correct_answer_json = question.correct_answer
    candidate.source_correct_answer_raw = question.source_correct_answer_raw
    candidate.true_label = question.true_label
    candidate.false_label = question.false_label
    candidate.explanation = question.explanation
    candidate.raw_ocr_text = question.raw_ocr_text
    candidate.edited_ocr_text = None
    candidate.normalized_ocr_text = question.normalized_ocr_text
    candidate.ocr_confidence = question.ocr_confidence
    candidate.parse_confidence = question.parse_confidence
    candidate.display_mode = question.display_mode
    candidate.anomaly_flags_json = [flag.model_dump(mode="json") for flag in question.anomaly_flags]
    candidate.status = "review" if question.anomaly_flags else "ready"


def _same_nonempty(first: str | None, second: str | None) -> bool:
    if first is None or second is None:
        return False
    left = first.strip()
    right = second.strip()
    return bool(left) and left == right


def _normalized_stem(value: str) -> str:
    return re.sub(r"\s+", "", normalize_ocr_text(value).casefold())


def _secondary_spatial_result(
    session: Session,
    secondary: QuestionCandidate,
) -> OcrPageResult:
    source_image = session.get(SourceImage, secondary.source_image_id)
    if source_image is None:
        raise ReviewValidationError("重复标题续页无法安全合并")
    try:
        return OcrPageResult.model_validate(source_image.ocr_result_json)
    except ValidationError as error:
        raise ReviewValidationError("重复标题续页无法安全合并") from error


def _reparse_complete_repeated_title_secondary(
    primary: QuestionCandidate,
    secondary: QuestionCandidate,
    source_result: OcrPageResult,
    *,
    low_confidence_threshold: float,
) -> ParsedQuestion:
    reparsed = parse_ocr_page(
        source_result,
        low_confidence_threshold=low_confidence_threshold,
    )
    matching_questions = [
        question
        for question in reparsed
        if _same_nonempty(
            primary.source_display_number,
            question.source_display_number,
        )
        and _same_nonempty(
            secondary.source_display_number,
            question.source_display_number,
        )
        and _same_nonempty(primary.question_type, question.question_type)
        and _same_nonempty(secondary.question_type, question.question_type)
        and question.raw_ocr_text == secondary.raw_ocr_text
    ]
    if len(matching_questions) != 1:
        raise ReviewValidationError("重复标题续页无法安全合并")
    question = matching_questions[0]
    identities_match = (
        _same_nonempty(primary.source_display_number, secondary.source_display_number)
        and _same_nonempty(
            primary.source_display_number,
            question.source_display_number,
        )
        and _same_nonempty(primary.question_type, secondary.question_type)
        and _same_nonempty(primary.question_type, question.question_type)
    )
    first_stem = _normalized_stem(primary.stem)
    complete_stem = _normalized_stem(question.stem)
    structurally_complete = (
        question.question_type != "unsupported"
        and not question.anomaly_flags
        and question.correct_answer is not None
        and len(question.options) >= 2
    )
    if (
        not identities_match
        or not structurally_complete
        or question.ocr_confidence != secondary.ocr_confidence
        or not first_stem
        or first_stem not in complete_stem
    ):
        raise ReviewValidationError("重复标题续页无法安全合并")
    return question




def _is_exact_confirmed_merge(
    relationship: CandidateRelationship,
    primary: QuestionCandidate,
    secondary: QuestionCandidate,
) -> bool:
    return (
        relationship.status == "resolved"
        and relationship.decision == "confirmed"
        and relationship.primary_candidate_id == primary.id
        and relationship.secondary_candidate_id == secondary.id
        and secondary.status == "merged_source"
    )


def _pending_relationship_snapshot(item: CandidateRelationship) -> dict:
    snapshot = _relationship_snapshot(item)
    snapshot["status"] = "pending"
    return snapshot


def _serialize_manual_merge_pair(
    session: Session,
    first_candidate_id: str,
    second_candidate_id: str,
) -> None:
    guard_candidate_id = min(first_candidate_id, second_candidate_id)
    session.execute(
        update(QuestionCandidate)
        .where(QuestionCandidate.id == guard_candidate_id)
        .values(updated_at=QuestionCandidate.updated_at)
        .execution_options(synchronize_session=False)
    )


def _merge_claimed_relationship(
    session: Session,
    relationship: CandidateRelationship,
    primary_id: str,
    secondary_id: str,
    *,
    relationship_created: bool,
    low_confidence_threshold: float,
) -> QuestionCandidate:
    if primary_id == secondary_id:
        raise ReviewValidationError("Cannot merge a candidate with itself")
    primary, secondary = _active_candidates(session, primary_id, secondary_id)
    if primary.batch_id != secondary.batch_id:
        raise ReviewValidationError("Candidates must belong to the same batch")
    if relationship.batch_id != primary.batch_id:
        raise ReviewValidationError("Candidate relationship does not exist")
    if {
        relationship.primary_candidate_id, relationship.secondary_candidate_id
    } != {primary.id, secondary.id}:
        raise ReviewValidationError("Candidates do not match the relationship")

    before = {
        "primary": capture_candidate(primary),
        "secondary": capture_candidate(secondary),
        "relationship": _pending_relationship_snapshot(relationship),
        "relationship_created": relationship_created,
    }
    original_display_mode = primary.display_mode
    original_explanation = primary.explanation or secondary.explanation
    combined_raw_text = f"{primary.raw_ocr_text}\n{secondary.raw_ocr_text}".strip()
    has_manual_update = session.scalar(
        select(ReviewAction.id)
        .where(
            ReviewAction.candidate_id == primary.id,
            ReviewAction.action_type == "update",
            ReviewAction.undone_at.is_(None),
        )
        .limit(1)
    ) is not None
    is_repeated_title = (
        relationship.relation_type == "cross_screenshot"
        and relationship.evidence_json.get("continuation_mode") == "repeated_title"
    )
    repeated_title_question: ParsedQuestion | None = None
    if (
        is_repeated_title
        and not has_manual_update
        and primary.edited_ocr_text is None
    ):
        repeated_title_question = _reparse_complete_repeated_title_secondary(
            primary,
            secondary,
            _secondary_spatial_result(session, secondary),
            low_confidence_threshold=low_confidence_threshold,
        )
    next_index = len(primary.image_parts)
    for offset, part in enumerate(
        sorted(secondary.image_parts, key=lambda item: item.part_index)
    ):
        session.add(
            CandidateImagePart(
                candidate_id=primary.id,
                source_image_id=part.source_image_id,
                crop_path=part.crop_path,
                source_y_start=part.source_y_start,
                source_y_end=part.source_y_end,
                part_index=next_index + offset,
            )
        )
    reparsed = parse_ocr_text(
        combined_raw_text,
        confidence=min(primary.ocr_confidence, secondary.ocr_confidence),
        low_confidence_threshold=low_confidence_threshold,
    )
    if repeated_title_question is not None:
        _apply_parsed(primary, repeated_title_question)
        primary.raw_ocr_text = combined_raw_text
        primary.display_mode = original_display_mode
        primary.explanation = primary.explanation or original_explanation
    elif (
        relationship.relation_type == "cross_screenshot"
        and not has_manual_update
        and primary.edited_ocr_text is None
        and not primary.options_json
        and primary.correct_answer_json is None
        and len(reparsed) == 1
        and reparsed[0].question_type != "unsupported"
        and len(reparsed[0].options) >= 2
        and reparsed[0].correct_answer is not None
    ):
        _apply_parsed(primary, reparsed[0])
        primary.display_mode = original_display_mode
        primary.explanation = primary.explanation or original_explanation
    else:
        if (
            not has_manual_update
            and primary.edited_ocr_text is None
            and secondary.stem
            and secondary.stem not in primary.stem
        ):
            primary.stem = f"{primary.stem}\n{secondary.stem}".strip()
        primary.raw_ocr_text = combined_raw_text
        primary.normalized_ocr_text = (
            f"{primary.normalized_ocr_text}\n{secondary.normalized_ocr_text}".strip()
        )
        primary.correct_answer_json = (
            primary.correct_answer_json
            if primary.correct_answer_json is not None
            else secondary.correct_answer_json
        )
        primary.source_correct_answer_raw = (
            primary.source_correct_answer_raw or secondary.source_correct_answer_raw
        )
        primary.explanation = original_explanation
        if not primary.options_json:
            primary.options_json = secondary.options_json
    primary.status = "review"
    secondary.status = "merged_source"
    relationship.decision = "confirmed"
    relationship.status = "resolved"
    session.flush()
    after = {
        "primary": capture_candidate(primary),
        "secondary": capture_candidate(secondary),
        "relationship": _relationship_snapshot(relationship),
        "relationship_created": relationship_created,
    }
    record_action(
        session,
        primary,
        "merge",
        before,
        after,
        target_type="relationship",
        target_id=relationship.id,
    )
    update_batch_review_state(session, primary.batch_id)
    session.commit()
    return _candidate(session, primary.id)


def _merge_candidates(
    session: Session,
    primary_id: str,
    secondary_id: str,
    relationship_id: str | None = None,
    *,
    low_confidence_threshold: float = 0.8,
) -> QuestionCandidate:
    if relationship_id is not None:
        relationship = session.get(CandidateRelationship, relationship_id)
        if relationship is not None:
            primary = _candidate(session, primary_id)
            secondary = _candidate(session, secondary_id)
            if _is_exact_confirmed_merge(relationship, primary, secondary):
                return primary
            if relationship.status == "pending":
                _require_active_candidates(primary, secondary)
        try:
            relationship = claim_pending_relationship(session, relationship_id)
        except ReviewRelationshipConflictError:
            session.rollback()
            relationship = session.get(CandidateRelationship, relationship_id)
            if relationship is None:
                raise ReviewValidationError("Candidate relationship does not exist")
            primary = _candidate(session, primary_id)
            secondary = _candidate(session, secondary_id)
            if _is_exact_confirmed_merge(relationship, primary, secondary):
                return primary
            raise
        return _merge_claimed_relationship(
            session,
            relationship,
            primary_id,
            secondary_id,
            relationship_created=False,
            low_confidence_threshold=low_confidence_threshold,
        )

    if primary_id == secondary_id:
        raise ReviewValidationError("Cannot merge a candidate with itself")
    primary, secondary = _active_candidates(session, primary_id, secondary_id)
    _serialize_manual_merge_pair(session, primary_id, secondary_id)
    if primary.batch_id != secondary.batch_id:
        raise ReviewValidationError("同一批次")
    relationship = session.scalar(
        select(CandidateRelationship)
        .where(
            CandidateRelationship.batch_id == primary.batch_id,
            or_(
                and_(
                    CandidateRelationship.primary_candidate_id == primary.id,
                    CandidateRelationship.secondary_candidate_id == secondary.id,
                ),
                and_(
                    CandidateRelationship.primary_candidate_id == secondary.id,
                    CandidateRelationship.secondary_candidate_id == primary.id,
                ),
            ),
        )
        .order_by(CandidateRelationship.created_at, CandidateRelationship.id)
    )
    if relationship is not None:
        existing_relationship_id = relationship.id
        session.rollback()
        return _merge_candidates(
            session,
            primary_id,
            secondary_id,
            existing_relationship_id,
            low_confidence_threshold=low_confidence_threshold,
        )

    relationship = CandidateRelationship(
        batch_id=primary.batch_id,
        primary_candidate_id=primary.id,
        secondary_candidate_id=secondary.id,
        relation_type="manual_merge",
        decision="suggested",
        confidence=1.0,
        evidence_json={"source": "manual_review"},
        status="pending",
    )
    session.add(relationship)
    session.flush()
    relationship = claim_pending_relationship(session, relationship.id)
    return _merge_claimed_relationship(
        session,
        relationship,
        primary_id,
        secondary_id,
        relationship_created=True,
        low_confidence_threshold=low_confidence_threshold,
    )


def merge_candidates(
    session: Session,
    primary_id: str,
    secondary_id: str,
    relationship_id: str | None = None,
    *,
    low_confidence_threshold: float = 0.8,
) -> QuestionCandidate:
    try:
        return _merge_candidates(
            session,
            primary_id,
            secondary_id,
            relationship_id,
            low_confidence_threshold=low_confidence_threshold,
        )
    except Exception:
        session.rollback()
        raise


def split_candidate(
    session: Session,
    settings: Settings,
    candidate_id: str,
    *,
    split_y: int,
    second_stem: str,
) -> QuestionCandidate:
    candidate = _candidate(session, candidate_id)
    if len(candidate.image_parts) != 1:
        raise ReviewValidationError("当前仅支持对单片段候选按裁切线拆分")
    part = candidate.image_parts[0]
    if not (part.source_y_start < split_y < part.source_y_end):
        raise ReviewValidationError("拆分线必须位于当前裁切范围内")
    if not second_stem.strip():
        raise ReviewValidationError("拆分后的第二题题干不能为空")
    before_primary = capture_candidate(candidate)
    original_end = part.source_y_end
    image = session.get(SourceImage, part.source_image_id)
    if image is None:
        raise ReviewValidationError("来源图片不存在")
    source = settings.data_dir / image.relative_path
    second = QuestionCandidate(
        batch_id=candidate.batch_id,
        category_id=candidate.category_id,
        source_image_id=candidate.source_image_id,
        source_display_number=candidate.source_display_number,
        question_type=candidate.question_type,
        stem=second_stem.strip(),
        options_json=list(candidate.options_json),
        correct_answer_json=candidate.correct_answer_json,
        source_correct_answer_raw=candidate.source_correct_answer_raw,
        true_label=candidate.true_label,
        false_label=candidate.false_label,
        explanation=candidate.explanation,
        raw_ocr_text=candidate.raw_ocr_text,
        edited_ocr_text=candidate.edited_ocr_text,
        normalized_ocr_text=candidate.normalized_ocr_text,
        ocr_confidence=candidate.ocr_confidence,
        parse_confidence=candidate.parse_confidence,
        display_mode=candidate.display_mode,
        anomaly_flags_json=[{"code": "MANUAL_SPLIT", "severity": "warning", "message": "由人工拆分，请复核字段"}],
        status="review",
    )
    file_transaction: CropFileTransaction | None = None
    try:
        part.source_y_end = split_y
        session.add(second)
        session.flush()
        second_crop = Path("crops") / candidate.batch_id / f"{second.id}.png"
        file_transaction = CropFileTransaction.stage(
            [
                CropReplacement(
                    source=source,
                    target=settings.data_dir / part.crop_path,
                    bounds=CropBounds(
                        y_start=part.source_y_start,
                        y_end=split_y,
                        page_height=image.height,
                    ),
                ),
                CropReplacement(
                    source=source,
                    target=settings.data_dir / second_crop,
                    bounds=CropBounds(
                        y_start=split_y,
                        y_end=original_end,
                        page_height=image.height,
                    ),
                ),
            ]
        )
        session.add(
            CandidateImagePart(
                candidate_id=second.id,
                source_image_id=image.id,
                crop_path=second_crop.as_posix(),
                source_y_start=split_y,
                source_y_end=original_end,
                part_index=0,
            )
        )
        session.flush()
        record_action(
            session,
            candidate,
            "split",
            {"primary": before_primary, "created_candidate_id": second.id},
            {
                "primary": capture_candidate(candidate),
                "created": capture_candidate(second),
            },
        )
        update_batch_review_state(session, candidate.batch_id)
        file_transaction.install()
        session.commit()
    except Exception:
        session.rollback()
        if file_transaction is not None:
            file_transaction.rollback()
        raise
    else:
        file_transaction.finalize()
    return _candidate(session, second.id)


def move_source_image(session: Session, candidate_id: str, direction: str) -> SourceImage:
    candidate = _candidate(session, candidate_id)
    images = list(
        session.scalars(
            select(SourceImage)
            .where(SourceImage.batch_id == candidate.batch_id)
            .order_by(SourceImage.order_index, SourceImage.id)
        )
    )
    current_index = next(index for index, image in enumerate(images) if image.id == candidate.source_image_id)
    offset = -1 if direction == "up" else 1 if direction == "down" else 0
    target_index = current_index + offset
    if offset == 0 or target_index < 0 or target_index >= len(images):
        raise ReviewValidationError("截图已经位于该方向的边界")
    before = {"orders": [{"id": image.id, "order_index": image.order_index} for image in images]}
    images[current_index].order_index, images[target_index].order_index = images[target_index].order_index, images[current_index].order_index
    session.flush()
    after = {"orders": [{"id": image.id, "order_index": image.order_index} for image in images]}
    record_action(session, candidate, "reorder", before, after, target_type="source_image", target_id=candidate.source_image_id)
    session.commit()
    return images[current_index]


def mark_duplicate(session: Session, candidate_id: str, duplicate_of_id: str) -> QuestionCandidate:
    candidate = _candidate(session, candidate_id)
    duplicate_of = _candidate(session, duplicate_of_id)
    _require_active_candidates(candidate, duplicate_of)
    if candidate.id == duplicate_of.id or candidate.batch_id != duplicate_of.batch_id:
        raise ReviewValidationError("只能标记同批次的另一道题为重复来源")
    before_candidate = capture_candidate(candidate)
    relationship = session.scalar(
        select(CandidateRelationship).where(
            CandidateRelationship.relation_type.in_({"manual_duplicate", "same_batch_duplicate"}),
            or_(
                and_(
                    CandidateRelationship.primary_candidate_id == duplicate_of.id,
                    CandidateRelationship.secondary_candidate_id == candidate.id,
                ),
                and_(
                    CandidateRelationship.primary_candidate_id == candidate.id,
                    CandidateRelationship.secondary_candidate_id == duplicate_of.id,
                ),
            ),
        )
    )
    relationship_existed = relationship is not None
    relationship_before = _relationship_snapshot(relationship) if relationship else None
    if relationship is None:
        relationship = CandidateRelationship(
            batch_id=candidate.batch_id,
            primary_candidate_id=duplicate_of.id,
            secondary_candidate_id=candidate.id,
            relation_type="manual_duplicate",
            decision="confirmed",
            confidence=1.0,
            evidence_json={"reviewed_manually": True},
            status="resolved",
        )
        session.add(relationship)
    else:
        relationship.primary_candidate_id = duplicate_of.id
        relationship.secondary_candidate_id = candidate.id
        relationship.decision = "confirmed"
        relationship.status = "resolved"
    candidate.duplicate_of_id = duplicate_of.id
    candidate.status = "duplicate_merged"
    session.flush()
    record_action(
        session,
        candidate,
        "duplicate",
        {
            "candidate": before_candidate,
            "relationship_existed": relationship_existed,
            "relationship": relationship_before,
            "relationship_id": relationship.id,
        },
        {"candidate": capture_candidate(candidate), "relationship": _relationship_snapshot(relationship)},
        target_type="relationship",
        target_id=relationship.id,
    )
    update_batch_review_state(session, candidate.batch_id)
    session.commit()
    return _candidate(session, candidate.id)


def rerun_candidate_ocr(
    session: Session,
    settings: Settings,
    engine: OcrEngine,
    candidate_id: str,
) -> QuestionCandidate:
    candidate = _candidate(session, candidate_id)
    if not candidate.image_parts:
        raise ReviewValidationError("候选题没有可识别的裁切片段")
    results = [engine.recognize(settings.data_dir / part.crop_path) for part in candidate.image_parts]
    parsed = [
        question
        for result in results
        for question in parse_ocr_page(
            result,
            low_confidence_threshold=settings.ocr_confidence_threshold,
        )
    ]
    if len(parsed) != 1:
        raise ReviewValidationError("单题 OCR 必须恰好识别出一道题")
    before = capture_candidate(candidate)
    _apply_parsed(candidate, parsed[0])
    if candidate.status == "ready" and has_pending_relationship(session, candidate.id):
        candidate.status = "review"
    session.flush()
    record_action(session, candidate, "rerun_candidate_ocr", before, capture_candidate(candidate))
    update_batch_review_state(session, candidate.batch_id)
    session.commit()
    return _candidate(session, candidate.id)




def _resolve_relationship(
    session: Session,
    relationship_id: str,
    decision: str,
    *,
    low_confidence_threshold: float = 0.8,
) -> CandidateRelationship:
    allowed_decisions = {
        "confirm",
        "swap",
        "only_primary",
        "only_secondary",
        "separate",
        "two_questions",
        "delete",
    }
    if decision not in allowed_decisions:
        raise ReviewValidationError("Relationship decision is invalid")

    try:
        relationship = claim_pending_relationship(session, relationship_id)
    except ReviewRelationshipConflictError:
        if decision != "confirm":
            raise
        session.rollback()
        relationship = session.get(CandidateRelationship, relationship_id)
        if relationship is None:
            raise ReviewValidationError("Candidate relationship does not exist")
        primary = _candidate(session, relationship.primary_candidate_id)
        secondary = _candidate(session, relationship.secondary_candidate_id)
        if _is_exact_confirmed_merge(relationship, primary, secondary):
            return relationship
        raise

    if decision == "confirm":
        _merge_claimed_relationship(
            session,
            relationship,
            relationship.primary_candidate_id,
            relationship.secondary_candidate_id,
            relationship_created=False,
            low_confidence_threshold=low_confidence_threshold,
        )
        return session.get(CandidateRelationship, relationship.id)

    primary = _candidate(session, relationship.primary_candidate_id)
    secondary = _candidate(session, relationship.secondary_candidate_id)
    before = {
        "relationship": _pending_relationship_snapshot(relationship),
        "primary": capture_candidate(primary),
        "secondary": capture_candidate(secondary),
    }
    if decision == "swap":
        relationship.primary_candidate_id, relationship.secondary_candidate_id = (
            relationship.secondary_candidate_id,
            relationship.primary_candidate_id,
        )
        relationship.decision = "suggested"
        relationship.status = "pending"
    elif decision in {"separate", "two_questions"}:
        relationship.decision = "kept_separate"
        relationship.status = "resolved"
    elif decision == "only_primary":
        secondary.status = "ignored"
        relationship.decision = "kept_primary"
        relationship.status = "resolved"
    elif decision == "only_secondary":
        primary.status = "ignored"
        relationship.decision = "kept_secondary"
        relationship.status = "resolved"
    else:
        secondary.status = "deleted"
        relationship.decision = "deleted_secondary"
        relationship.status = "resolved"
    session.flush()
    record_action(
        session,
        primary,
        "relationship",
        before,
        {
            "relationship": _relationship_snapshot(relationship),
            "primary": capture_candidate(primary),
            "secondary": capture_candidate(secondary),
        },
        target_type="relationship",
        target_id=relationship.id,
    )
    update_batch_review_state(session, relationship.batch_id)
    session.commit()
    return session.get(CandidateRelationship, relationship.id)


def resolve_relationship(
    session: Session,
    relationship_id: str,
    decision: str,
    *,
    low_confidence_threshold: float = 0.8,
) -> CandidateRelationship:
    try:
        return _resolve_relationship(
            session,
            relationship_id,
            decision,
            low_confidence_threshold=low_confidence_threshold,
        )
    except Exception:
        session.rollback()
        raise
