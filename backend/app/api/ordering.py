from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings
from app.database import get_session
from app.models import (
    ImportBatch,
    OrderingIssue,
    ScreenshotOverlap,
    SourceImage,
)
from app.schemas.ordering import (
    OrderingImageResponse,
    OrderingIssueResponse,
    OrderingResponse,
    OrderingUpdate,
    OverlapResponse,
)
from app.services.ordering_exclusions import reconcile_excluded_sources


router = APIRouter(prefix="/import-jobs/{job_id}", tags=["截图顺序"])


def _batch_or_404(session: Session, job_id: str) -> ImportBatch:
    batch = session.scalar(
        select(ImportBatch)
        .options(selectinload(ImportBatch.images))
        .where(ImportBatch.id == job_id)
    )
    if batch is None:
        raise HTTPException(status_code=404, detail="导入任务不存在")
    return batch


def _serialize(session: Session, batch: ImportBatch) -> OrderingResponse:
    images = sorted(batch.images, key=lambda item: (item.order_index, item.id))
    overlaps = list(
        session.scalars(
            select(ScreenshotOverlap)
            .where(ScreenshotOverlap.batch_id == batch.id)
            .order_by(ScreenshotOverlap.combined_score.desc())
        )
    )
    issues = list(
        session.scalars(
            select(OrderingIssue)
            .where(OrderingIssue.batch_id == batch.id)
            .order_by(OrderingIssue.created_at, OrderingIssue.id)
        )
    )
    return OrderingResponse(
        batch_id=batch.id,
        status=batch.status,
        requires_review=batch.status == "ordering_review",
        images=[
            OrderingImageResponse(
                id=image.id,
                original_name=image.original_name,
                order_index=image.order_index,
                review_flag=image.review_flag,
                duplicate_of_id=image.duplicate_of_id,
                content_url=f"/api/import-jobs/{batch.id}/images/{image.id}/content",
            )
            for image in images
        ],
        overlaps=[
            OverlapResponse(
                source_image_id=item.source_image_id,
                target_image_id=item.target_image_id,
                text_score=item.text_score,
                visual_score=item.visual_score,
                combined_score=item.combined_score,
                overlap_ratio=item.overlap_ratio,
                matched_text=item.matched_text_json,
            )
            for item in overlaps
        ],
        issues=[
            OrderingIssueResponse(
                id=item.id,
                code=item.code,
                message=item.message,
                image_ids=item.image_ids_json,
                status=item.status,
            )
            for item in issues
        ],
    )


@router.get("/ordering", response_model=OrderingResponse)
def get_ordering(job_id: str, session: Session = Depends(get_session)) -> OrderingResponse:
    return _serialize(session, _batch_or_404(session, job_id))


@router.get("/images/{image_id}/content", response_class=FileResponse)
def get_image_content(
    request: Request,
    job_id: str,
    image_id: str,
    session: Session = Depends(get_session),
) -> FileResponse:
    _batch_or_404(session, job_id)
    image = session.scalar(
        select(SourceImage).where(
            SourceImage.id == image_id, SourceImage.batch_id == job_id
        )
    )
    if image is None:
        raise HTTPException(status_code=404, detail="来源图片不存在")
    settings: Settings = request.app.state.settings
    root = settings.data_dir.resolve()
    path = (settings.data_dir / Path(image.relative_path)).resolve()
    if not path.is_file() or root not in path.parents:
        raise HTTPException(status_code=404, detail="来源图片不可用")
    return FileResponse(path, media_type=image.mime_type)


@router.put("/ordering", response_model=OrderingResponse)
def confirm_ordering(
    job_id: str,
    payload: OrderingUpdate,
    session: Session = Depends(get_session),
) -> OrderingResponse:
    batch = _batch_or_404(session, job_id)
    if batch.status != "ordering_review":
        raise HTTPException(
            status_code=409,
            detail=f"导入任务当前状态不能确认截图顺序：{batch.status}",
        )
    expected_ids = {image.id for image in batch.images}
    submitted_ids = [item.image_id for item in payload.items]
    if len(submitted_ids) != len(set(submitted_ids)):
        raise HTTPException(status_code=400, detail="截图顺序中存在重复图片")
    if set(submitted_ids) != expected_ids:
        raise HTTPException(status_code=400, detail="截图顺序必须完整包含该批次全部图片")
    by_id = {image.id: image for image in batch.images}
    for index, item in enumerate(payload.items):
        image = by_id[item.image_id]
        image.order_index = index
        image.review_flag = item.review_flag
    for issue in session.scalars(
        select(OrderingIssue).where(OrderingIssue.batch_id == job_id)
    ):
        issue.status = "resolved"
    reconcile_excluded_sources(session, job_id)
    session.commit()
    return _serialize(session, _batch_or_404(session, job_id))
