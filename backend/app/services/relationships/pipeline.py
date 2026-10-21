from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from itertools import combinations, permutations
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.database import Database
from app.models import (
    CandidateRelationship,
    ImportBatch,
    OrderingIssue,
    QuestionCandidate,
    ScreenshotOverlap,
    SourceImage,
)
from app.ocr.base import OcrPageResult
from app.services.relationships.cross_screenshot import (
    CrossScreenshotAssessment,
    assess_cross_screenshot,
)
from app.services.relationships.duplicates import assess_duplicate, normalized_content
from app.services.relationships.ordering import resolve_order
from app.services.relationships.overlap import OverlapScore, score_directional_overlap


@dataclass(frozen=True, slots=True)
class RelationshipSummary:
    order_auto_applied: bool
    overlap_count: int
    ordering_issue_count: int
    auto_duplicate_count: int
    duplicate_review_count: int
    cross_screenshot_suggestions: int


def _candidate_text(candidate: QuestionCandidate) -> str:
    options = "|".join(
        f"{item.get('label', '')}{item.get('text', '')}"
        for item in candidate.options_json
    )
    return f"{candidate.stem}|{options}"


def _fingerprint(candidate: QuestionCandidate) -> str:
    payload = {
        "text": normalized_content(_candidate_text(candidate)),
        "answer": candidate.correct_answer_json,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _crop_path(candidate: QuestionCandidate, settings: Settings) -> Path | None:
    if not candidate.image_parts:
        return None
    path = settings.data_dir / Path(candidate.image_parts[0].crop_path)
    return path if path.is_file() else None


def _flags(candidate: QuestionCandidate) -> set[str]:
    return {
        str(item.get("code"))
        for item in candidate.anomaly_flags_json
        if item.get("code")
    }


def _candidate_pair_key(first_id: str, second_id: str) -> tuple[str, str]:
    if first_id <= second_id:
        return first_id, second_id
    return second_id, first_id


def analyze_batch_relationships(
    database: Database, settings: Settings, batch_id: str
) -> RelationshipSummary:
    with database.session_factory() as session:
        batch = session.scalar(
            select(ImportBatch)
            .options(
                selectinload(ImportBatch.images),
            )
            .where(ImportBatch.id == batch_id)
        )
        if batch is None:
            raise ValueError("导入任务不存在")
        session.execute(delete(ScreenshotOverlap).where(ScreenshotOverlap.batch_id == batch_id))
        session.execute(delete(OrderingIssue).where(OrderingIssue.batch_id == batch_id))
        session.execute(delete(CandidateRelationship).where(CandidateRelationship.batch_id == batch_id))

        images = [
            image
            for image in batch.images
            if image.duplicate_of_id is None
            and image.ocr_status == "completed"
            and isinstance(image.ocr_result_json, dict)
        ]
        for image in batch.images:
            if image.duplicate_of_id is not None:
                image.review_flag = "duplicate"
        scores: list[OverlapScore] = []
        for first, second in permutations(images, 2):
            first_path = settings.data_dir / Path(first.relative_path)
            second_path = settings.data_dir / Path(second.relative_path)
            score = score_directional_overlap(
                OcrPageResult.model_validate(first.ocr_result_json),
                OcrPageResult.model_validate(second.ocr_result_json),
                first_path,
                second_path,
                settings,
                source_id=first.id,
                target_id=second.id,
            )
            scores.append(score)
            if score.combined_score >= settings.overlap_candidate_threshold:
                session.add(
                    ScreenshotOverlap(
                        batch_id=batch_id,
                        source_image_id=first.id,
                        target_image_id=second.id,
                        text_score=score.text_score,
                        visual_score=score.visual_score,
                        combined_score=score.combined_score,
                        overlap_ratio=score.overlap_ratio,
                        matched_text_json=score.matched_text,
                    )
                )

        resolution = resolve_order([image.id for image in images], scores, settings)
        if resolution.auto_applied:
            by_id = {image.id: image for image in images}
            for index, image_id in enumerate(resolution.ordered_ids):
                by_id[image_id].order_index = index
        else:
            for issue in resolution.issues:
                session.add(
                    OrderingIssue(
                        batch_id=batch_id,
                        code=issue.code,
                        message=issue.message,
                        image_ids_json=issue.image_ids,
                    )
                )

        candidates = list(
            session.scalars(
                select(QuestionCandidate)
                .options(selectinload(QuestionCandidate.image_parts))
                .where(QuestionCandidate.batch_id == batch_id)
                .order_by(QuestionCandidate.created_at, QuestionCandidate.id)
            )
        )
        for item in candidates:
            item.content_fingerprint = _fingerprint(item)
            item.duplicate_of_id = None

        by_source: dict[str, list[QuestionCandidate]] = {}
        for item in candidates:
            by_source.setdefault(item.source_image_id, []).append(item)
        score_by_pair = {(item.source_id, item.target_id): item for item in scores}
        order = (
            resolution.ordered_ids
            if resolution.auto_applied
            else [
                image.id
                for image in sorted(images, key=lambda image: image.order_index)
            ]
        )
        cross_suggestions: list[
            tuple[str, str, CrossScreenshotAssessment, str, str]
        ] = []
        cross_pair_keys: set[tuple[str, str]] = set()
        for first_image_id, second_image_id in zip(order, order[1:]):
            overlap = score_by_pair.get((first_image_id, second_image_id))
            if overlap is None:
                continue
            for first in by_source.get(first_image_id, []):
                for second in by_source.get(second_image_id, []):
                    assessment = assess_cross_screenshot(
                        _flags(first),
                        _flags(second),
                        overlap,
                        first.raw_ocr_text,
                        second.raw_ocr_text,
                        settings,
                        first_display_number=first.source_display_number,
                        second_display_number=second.source_display_number,
                        first_question_type=first.question_type,
                        second_question_type=second.question_type,
                    )
                    if assessment is None:
                        continue
                    pair_key = _candidate_pair_key(first.id, second.id)
                    if pair_key in cross_pair_keys:
                        continue
                    cross_pair_keys.add(pair_key)
                    cross_suggestions.append(
                        (
                            first.id,
                            second.id,
                            assessment,
                            first_image_id,
                            second_image_id,
                        )
                    )

        auto_duplicates = 0
        duplicate_reviews = 0
        for first, second in combinations(candidates, 2):
            if _candidate_pair_key(first.id, second.id) in cross_pair_keys:
                continue
            first_crop = _crop_path(first, settings)
            second_crop = _crop_path(second, settings)
            if first_crop is None or second_crop is None:
                continue
            assessment = assess_duplicate(
                _candidate_text(first),
                _candidate_text(second),
                first_crop,
                second_crop,
                first.correct_answer_json,
                second.correct_answer_json,
                True,
                settings,
            )
            if assessment is None:
                continue
            session.add(
                CandidateRelationship(
                    batch_id=batch_id,
                    primary_candidate_id=first.id,
                    secondary_candidate_id=second.id,
                    relation_type=assessment.relation_type,
                    decision=assessment.decision,
                    confidence=assessment.confidence,
                    evidence_json={
                        **assessment.evidence,
                        "text_score": assessment.text_score,
                        "hash_distance": assessment.hash_distance,
                    },
                    status="resolved" if assessment.decision == "auto_merged" else "pending",
                )
            )
            if assessment.decision == "auto_merged":
                second.duplicate_of_id = first.id
                second.status = "duplicate_merged"
                auto_duplicates += 1
            else:
                second.status = "review"
                duplicate_reviews += 1

        session.flush()
        for current in candidates:
            if not current.content_fingerprint:
                continue
            current_crop = _crop_path(current, settings)
            if current_crop is None:
                continue
            matches = list(
                session.scalars(
                    select(QuestionCandidate)
                    .options(selectinload(QuestionCandidate.image_parts))
                    .where(
                        QuestionCandidate.batch_id != batch_id,
                        QuestionCandidate.content_fingerprint == current.content_fingerprint,
                    )
                )
            )
            for previous in matches:
                previous_crop = _crop_path(previous, settings)
                if previous_crop is None:
                    continue
                assessment = assess_duplicate(
                    _candidate_text(current),
                    _candidate_text(previous),
                    current_crop,
                    previous_crop,
                    current.correct_answer_json,
                    previous.correct_answer_json,
                    False,
                    settings,
                )
                if assessment:
                    session.add(
                        CandidateRelationship(
                            batch_id=batch_id,
                            primary_candidate_id=current.id,
                            secondary_candidate_id=previous.id,
                            relation_type=assessment.relation_type,
                            decision="kept_separate",
                            confidence=assessment.confidence,
                            evidence_json=assessment.evidence,
                            status="resolved",
                        )
                    )

        cross_count = 0
        for (
            first_candidate_id,
            second_candidate_id,
            assessment,
            first_image_id,
            second_image_id,
        ) in cross_suggestions:
            session.add(
                CandidateRelationship(
                    batch_id=batch_id,
                    primary_candidate_id=first_candidate_id,
                    secondary_candidate_id=second_candidate_id,
                    relation_type="cross_screenshot",
                    decision="suggested",
                    confidence=assessment.confidence,
                    evidence_json={
                        "merged_text_preview": assessment.merged_text_preview,
                        "requires_confirmation": True,
                        "continuation_mode": assessment.continuation_mode,
                        "source_image_ids": [first_image_id, second_image_id],
                    },
                    status="pending",
                )
            )
            cross_count += 1

        batch.found_question_count = sum(
            item.status != "duplicate_merged" for item in candidates
        )
        batch.high_confidence_count = sum(item.status == "ready" for item in candidates)
        batch.review_count = sum(item.status == "review" for item in candidates)
        if not resolution.auto_applied:
            batch.status = "ordering_review"
        elif cross_count or duplicate_reviews or any(item.status == "review" for item in candidates):
            batch.status = "question_review"
        else:
            batch.status = "ready"
        session.commit()
        return RelationshipSummary(
            order_auto_applied=resolution.auto_applied,
            overlap_count=sum(
                1
                for score in scores
                if score.combined_score >= settings.overlap_candidate_threshold
            ),
            ordering_issue_count=len(resolution.issues),
            auto_duplicate_count=auto_duplicates,
            duplicate_review_count=duplicate_reviews,
            cross_screenshot_suggestions=cross_count,
        )
