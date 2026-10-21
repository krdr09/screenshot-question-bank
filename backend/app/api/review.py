from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import get_session
from app.models import CandidateImagePart, CandidateRelationship, Category, Course, ImportBatch, OrderingIssue, QuestionCandidate, ReviewAction, SourceImage
from app.schemas.review import ConfirmedAction, DuplicateAction, MergeAction, RelationshipAction, RelationshipCropUpdate, ReorderAction, ReviewCandidateUpdate, SplitAction
from app.services.review.actions import mark_duplicate, merge_candidates, move_source_image, rerun_candidate_ocr, resolve_relationship, split_candidate
from app.services.review.editor import ReviewValidationError, confirm_candidate, delete_candidate, ignore_candidate, undo_last_action, update_candidate, update_relationship_crops
from app.services.review.query import ReviewFilters, ReviewItem, get_review_item, list_batch_context_items, list_review_items
from app.services.review.relationship_state import ReviewRelationshipConflictError


router = APIRouter(tags=["异常题复核"])


def _error(error: ReviewValidationError) -> HTTPException:
    if isinstance(error, ReviewRelationshipConflictError):
        return HTTPException(status_code=409, detail=str(error))
    status = 404 if "不存在" in str(error) else 422
    return HTTPException(status_code=status, detail=str(error))


def _relationship_dict(session: Session, item: CandidateRelationship) -> dict:
    batch = session.get(ImportBatch, item.batch_id)
    return {
        "id": item.id,
        "batch_id": item.batch_id,
        "batch_name": batch.name if batch else "",
        "course_name": batch.course.name if batch and batch.course else "",
        "category_path": batch.category.path if batch and batch.category else "",
        "relation_type": item.relation_type,
        "primary_candidate_id": item.primary_candidate_id,
        "secondary_candidate_id": item.secondary_candidate_id,
        "decision": item.decision,
        "confidence": item.confidence,
        "evidence": item.evidence_json,
        "status": item.status,
    }


def _ordering_issue_dict(session: Session, item: OrderingIssue) -> dict:
    batch = session.get(ImportBatch, item.batch_id)
    return {
        "id": item.id,
        "batch_id": item.batch_id,
        "batch_name": batch.name if batch else "",
        "course_name": batch.course.name if batch and batch.course else "",
        "category_path": batch.category.path if batch and batch.category else "",
        "code": item.code,
        "message": item.message,
        "image_ids": item.image_ids_json,
        "status": item.status,
    }


def _serialize(session: Session, item: ReviewItem) -> dict:
    candidate = item.candidate
    relationships = list(
        session.scalars(
            select(CandidateRelationship)
            .where(
                or_(
                    CandidateRelationship.primary_candidate_id == candidate.id,
                    CandidateRelationship.secondary_candidate_id == candidate.id,
                )
            )
            .order_by(CandidateRelationship.created_at, CandidateRelationship.id)
        )
    )
    can_undo = session.scalar(
        select(ReviewAction.id)
        .where(ReviewAction.candidate_id == candidate.id, ReviewAction.undone_at.is_(None))
        .limit(1)
    ) is not None
    return {
        "id": candidate.id,
        "batch_id": candidate.batch_id,
        "batch_name": item.batch_name,
        "course_name": item.course_name,
        "category_path": item.category_path,
        "source_image_id": candidate.source_image_id,
        "source_image_name": candidate.source_image.original_name,
        "source_display_number": candidate.source_display_number,
        "question_type": candidate.question_type,
        "stem": candidate.stem,
        "options": candidate.options_json,
        "correct_answer": candidate.correct_answer_json,
        "true_label": candidate.true_label,
        "false_label": candidate.false_label,
        "explanation": candidate.explanation,
        "raw_ocr_text": candidate.raw_ocr_text,
        "edited_ocr_text": candidate.edited_ocr_text,
        "ocr_confidence": candidate.ocr_confidence,
        "parse_confidence": candidate.parse_confidence,
        "display_mode": candidate.display_mode,
        "anomaly_flags": candidate.anomaly_flags_json,
        "status": candidate.status,
        "duplicate_of_id": candidate.duplicate_of_id,
        "parts": [
            {
                "id": part.id,
                "source_image_id": part.source_image_id,
                "part_index": part.part_index,
                "source_y_start": part.source_y_start,
                "source_y_end": part.source_y_end,
                "source_height": (session.get(SourceImage, part.source_image_id).height if session.get(SourceImage, part.source_image_id) else part.source_y_end),
                "crop_url": f"/api/review-items/{candidate.id}/parts/{part.id}/content",
                "source_url": f"/api/review-items/{candidate.id}/source-images/{part.source_image_id}/content",
            }
            for part in candidate.image_parts
        ],
        "relationships": [_relationship_dict(session, relationship) for relationship in relationships],
        "can_undo": can_undo,
    }


def _item_or_404(session: Session, candidate_id: str) -> ReviewItem:
    item = get_review_item(session, candidate_id)
    if item is None:
        raise HTTPException(status_code=404, detail="候选题不存在")
    return item


@router.get("/review-items")
def review_items(
    batch_id: str | None = None,
    course_id: str | None = None,
    category_id: str | None = None,
    course_name: str | None = None,
    category_path: str | None = None,
    batch_name: str | None = None,
    anomaly_code: str | None = None,
    question_type: str | None = None,
    severity: str | None = None,
    handling_status: str = Query(default="pending", pattern="^(pending|processed|all)$"),
    session: Session = Depends(get_session),
) -> dict:
    filters = ReviewFilters(batch_id=batch_id, course_id=course_id, category_id=category_id, course_name=course_name, category_path=category_path, batch_name=batch_name, anomaly_code=anomaly_code, question_type=question_type, severity=severity, handling_status=handling_status)
    items = list_review_items(session, filters)
    scope = (
        select(ImportBatch.id)
        .join(Course, Course.id == ImportBatch.course_id)
        .outerjoin(Category, Category.id == ImportBatch.category_id)
    )
    if batch_id:
        scope = scope.where(ImportBatch.id == batch_id)
    if course_id:
        scope = scope.where(ImportBatch.course_id == course_id)
    if category_id:
        scope = scope.where(ImportBatch.category_id == category_id)
    if course_name:
        scope = scope.where(Course.name == course_name)
    if category_path:
        scope = scope.where(Category.path == category_path)
    if batch_name:
        scope = scope.where(ImportBatch.name == batch_name)
    batch_ids = set(session.scalars(scope))
    relationship_statement = select(CandidateRelationship).where(
        CandidateRelationship.batch_id.in_(batch_ids)
    )
    issue_statement = select(OrderingIssue).where(OrderingIssue.batch_id.in_(batch_ids))
    if handling_status == "pending":
        relationship_statement = relationship_statement.where(CandidateRelationship.status == "pending")
        issue_statement = issue_statement.where(OrderingIssue.status == "pending")
    elif handling_status == "processed":
        relationship_statement = relationship_statement.where(CandidateRelationship.status != "pending")
        issue_statement = issue_statement.where(OrderingIssue.status != "pending")

    relationships = list(
        session.scalars(
            relationship_statement.order_by(
                CandidateRelationship.created_at, CandidateRelationship.id
            )
        )
    ) if batch_ids else []
    if anomaly_code or severity or question_type:
        filtered_relationships: list[CandidateRelationship] = []
        for relationship in relationships:
            candidates = [
                session.get(QuestionCandidate, relationship.primary_candidate_id),
                session.get(QuestionCandidate, relationship.secondary_candidate_id),
            ]
            candidates = [candidate for candidate in candidates if candidate is not None]
            if question_type and not any(
                candidate.question_type == question_type for candidate in candidates
            ):
                continue
            if anomaly_code and not any(
                any(flag.get("code") == anomaly_code for flag in (candidate.anomaly_flags_json or []))
                for candidate in candidates
            ):
                continue
            if severity and not any(
                any(flag.get("severity") == severity for flag in (candidate.anomaly_flags_json or []))
                for candidate in candidates
            ):
                continue
            filtered_relationships.append(relationship)
        relationships = filtered_relationships

    issues = [] if (anomaly_code or severity or question_type) else list(
        session.scalars(
            issue_statement.order_by(OrderingIssue.created_at, OrderingIssue.id)
        )
    ) if batch_ids else []
    return {
        "items": [_serialize(session, item) for item in items],
        "relationships": [_relationship_dict(session, item) for item in relationships],
        "ordering_issues": [_ordering_issue_dict(session, issue) for issue in issues],
    }


@router.get("/review-items/{candidate_id}/batch-context")
def review_batch_context(
    candidate_id: str,
    session: Session = Depends(get_session),
) -> dict:
    items = list_batch_context_items(session, candidate_id)
    if items is None:
        raise HTTPException(status_code=404, detail="候选题不存在")
    return {
        "items": [
            {
                "id": item.id,
                "batch_id": item.batch_id,
                "stem": item.stem,
                "status": item.status,
            }
            for item in items
        ]
    }


@router.get("/review-items/{candidate_id}")
def review_item(candidate_id: str, session: Session = Depends(get_session)) -> dict:
    return _serialize(session, _item_or_404(session, candidate_id))


@router.patch("/review-items/{candidate_id}")
def patch_review_item(request: Request, candidate_id: str, payload: ReviewCandidateUpdate, session: Session = Depends(get_session)) -> dict:
    try:
        candidate = update_candidate(session, request.app.state.settings, candidate_id, payload.model_dump(exclude_unset=True))
    except ReviewValidationError as error:
        raise _error(error) from error
    return _serialize(session, _item_or_404(session, candidate.id))


def _safe_file(settings: Settings, relative_path: str) -> Path:
    root = settings.data_dir.resolve()
    path = (settings.data_dir / Path(relative_path)).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="本地图片不可用")
    return path


@router.get("/review-items/{candidate_id}/parts/{part_id}/content", response_class=FileResponse)
def review_crop(request: Request, candidate_id: str, part_id: str, session: Session = Depends(get_session)) -> FileResponse:
    _item_or_404(session, candidate_id)
    part = session.scalar(select(CandidateImagePart).where(CandidateImagePart.id == part_id, CandidateImagePart.candidate_id == candidate_id))
    if part is None:
        raise HTTPException(status_code=404, detail="裁切片段不存在")
    return FileResponse(_safe_file(request.app.state.settings, part.crop_path), media_type="image/png")


@router.get("/review-items/{candidate_id}/source-images/{image_id}/content", response_class=FileResponse)
def review_source(request: Request, candidate_id: str, image_id: str, session: Session = Depends(get_session)) -> FileResponse:
    item = _item_or_404(session, candidate_id)
    allowed = {part.source_image_id for part in item.candidate.image_parts}
    if image_id not in allowed:
        raise HTTPException(status_code=404, detail="来源图片不属于该候选题")
    image = session.get(SourceImage, image_id)
    if image is None or image.batch_id != item.candidate.batch_id:
        raise HTTPException(status_code=404, detail="来源图片不存在")
    return FileResponse(_safe_file(request.app.state.settings, image.relative_path), media_type=image.mime_type)


def _action_result(session: Session, candidate_id: str, function) -> dict:
    try:
        candidate = function()
    except ReviewValidationError as error:
        raise _error(error) from error
    return _serialize(session, _item_or_404(session, candidate.id))


@router.post("/review-items/{candidate_id}/confirm")
def confirm(candidate_id: str, session: Session = Depends(get_session)) -> dict:
    return _action_result(session, candidate_id, lambda: confirm_candidate(session, candidate_id))


@router.post("/review-items/{candidate_id}/ignore")
def ignore(candidate_id: str, _: ConfirmedAction, session: Session = Depends(get_session)) -> dict:
    return _action_result(session, candidate_id, lambda: ignore_candidate(session, candidate_id))


@router.post("/review-items/{candidate_id}/delete")
def delete(candidate_id: str, _: ConfirmedAction, session: Session = Depends(get_session)) -> dict:
    return _action_result(session, candidate_id, lambda: delete_candidate(session, candidate_id))


@router.delete("/review-items/{candidate_id}")
def delete_required(candidate_id: str, _: ConfirmedAction, session: Session = Depends(get_session)) -> dict:
    return _action_result(session, candidate_id, lambda: delete_candidate(session, candidate_id))


@router.post("/review-items/{candidate_id}/undo")
def undo(request: Request, candidate_id: str, session: Session = Depends(get_session)) -> dict:
    return _action_result(session, candidate_id, lambda: undo_last_action(session, request.app.state.settings, candidate_id))


@router.post("/review-items/{candidate_id}/duplicate")
def duplicate(candidate_id: str, payload: DuplicateAction, session: Session = Depends(get_session)) -> dict:
    return _action_result(session, candidate_id, lambda: mark_duplicate(session, candidate_id, payload.duplicate_of_id))


@router.post("/review-items/{candidate_id}/merge")
def merge(
    request: Request,
    candidate_id: str,
    payload: MergeAction,
    session: Session = Depends(get_session),
) -> dict:
    return _action_result(
        session,
        candidate_id,
        lambda: merge_candidates(
            session,
            candidate_id,
            payload.secondary_candidate_id,
            payload.relationship_id,
            low_confidence_threshold=(
                request.app.state.settings.ocr_confidence_threshold
            ),
        ),
    )


@router.post("/review-items/{candidate_id}/split")
def split(request: Request, candidate_id: str, payload: SplitAction, session: Session = Depends(get_session)) -> dict:
    return _action_result(session, candidate_id, lambda: split_candidate(session, request.app.state.settings, candidate_id, split_y=payload.split_y, second_stem=payload.second_stem))


@router.post("/review-items/{candidate_id}/reorder")
def reorder(candidate_id: str, payload: ReorderAction, session: Session = Depends(get_session)) -> dict:
    return _action_result(session, candidate_id, lambda: (move_source_image(session, candidate_id, payload.direction), _item_or_404(session, candidate_id).candidate)[1])


@router.post("/review-items/{candidate_id}/rerun-ocr")
def rerun_ocr(request: Request, candidate_id: str, _: ConfirmedAction, session: Session = Depends(get_session)) -> dict:
    engine = request.app.state.ocr_worker.engine
    return _action_result(session, candidate_id, lambda: rerun_candidate_ocr(session, request.app.state.settings, engine, candidate_id))


@router.post("/review-items/{candidate_id}/rerun-source-ocr")
def rerun_source_ocr(request: Request, candidate_id: str, _: ConfirmedAction, session: Session = Depends(get_session)) -> dict:
    item = _item_or_404(session, candidate_id)
    try:
        request.app.state.ocr_worker.reprocess_image(
            item.candidate.source_image_id, audit_candidate_id=candidate_id
        )
    except (ValueError, RuntimeError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    session.expire_all()
    return _serialize(session, _item_or_404(session, candidate_id))


@router.post("/review-relationships/{relationship_id}/resolve")
def relationship_action(
    request: Request,
    relationship_id: str,
    payload: RelationshipAction,
    session: Session = Depends(get_session),
) -> dict:
    try:
        relationship = resolve_relationship(
            session,
            relationship_id,
            payload.decision,
            low_confidence_threshold=(
                request.app.state.settings.ocr_confidence_threshold
            ),
        )
    except ReviewValidationError as error:
        raise _error(error) from error
    return _relationship_dict(session, relationship)


@router.patch("/review-relationships/{relationship_id}/crops")
def patch_relationship_crops(
    request: Request,
    relationship_id: str,
    payload: RelationshipCropUpdate,
    session: Session = Depends(get_session),
) -> dict:
    try:
        primary, secondary = update_relationship_crops(
            session,
            request.app.state.settings,
            relationship_id,
            [item.model_dump() for item in payload.primary_parts],
            [item.model_dump() for item in payload.secondary_parts],
        )
    except ReviewValidationError as error:
        raise _error(error) from error
    return {
        "primary": _serialize(session, _item_or_404(session, primary.id)),
        "secondary": _serialize(session, _item_or_404(session, secondary.id)),
    }
