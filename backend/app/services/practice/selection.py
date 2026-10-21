from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Literal, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Attempt,
    Category,
    Course,
    ImportBatch,
    PracticeItem,
    QuestionCandidate,
    SourceImage,
    WrongBookEntry,
)
from app.services.candidate_eligibility import ready_candidate_predicate


class PracticeSelectionError(ValueError):
    pass


class ShuffleSource(Protocol):
    def shuffle(self, values: list[SelectedQuestion]) -> None: ...


@dataclass(frozen=True)
class SelectionCriteria:
    course_id: str | None = None
    category_id: str | None = None
    batch_id: str | None = None
    question_type: str | None = None
    membership: Literal["all", "wrong", "unanswered"] = "all"
    manual_question_ids: tuple[str, ...] = ()
    position_start: int | None = None
    position_end: int | None = None


@dataclass(frozen=True)
class SelectedQuestion:
    question: QuestionCandidate
    course_id: str
    course_name: str
    category_id: str | None
    category_path: str
    batch_id: str
    batch_name: str
    batch_position: int
    in_wrong_book: bool
    has_answered: bool
    batch_created_at_key: str


@dataclass(frozen=True)
class _ReadyRow:
    question: QuestionCandidate
    course_id: str
    course_name: str
    category_id: str | None
    category_path: str
    batch_id: str
    batch_name: str
    batch_created_at_key: str
    in_wrong_book: bool
    has_answered: bool
    source_order: int
    source_y_start: int


def _validate(criteria: SelectionCriteria) -> None:
    if criteria.membership not in {"all", "wrong", "unanswered"}:
        raise PracticeSelectionError("刷题成员范围无效")
    if criteria.question_type not in {None, "single_choice", "true_false"}:
        raise PracticeSelectionError("仅支持单选题和判断题")
    has_start = criteria.position_start is not None
    has_end = criteria.position_end is not None
    if has_start != has_end:
        raise PracticeSelectionError("批次内部位置必须同时填写起点和终点")
    if has_start:
        assert criteria.position_start is not None
        assert criteria.position_end is not None
        if criteria.batch_id is None:
            raise PracticeSelectionError("按内部位置选题时必须指定一个导入批次")
        if criteria.position_start < 1 or criteria.position_end < criteria.position_start:
            raise PracticeSelectionError("批次内部位置范围无效")
    if criteria.manual_question_ids and has_start:
        raise PracticeSelectionError("手动选题和批次位置范围不能同时使用")


def _ready_rows(session: Session) -> list[_ReadyRow]:
    effective_category = func.coalesce(
        QuestionCandidate.category_id, ImportBatch.category_id
    )
    rows = session.execute(
        select(
            QuestionCandidate,
            Course.id,
            Course.name,
            Category.id,
            Category.path,
            ImportBatch.id,
            ImportBatch.name,
            ImportBatch.created_at,
            WrongBookEntry.id,
            WrongBookEntry.mastered_at,
        )
        .join(ImportBatch, ImportBatch.id == QuestionCandidate.batch_id)
        .join(Course, Course.id == ImportBatch.course_id)
        .outerjoin(Category, Category.id == effective_category)
        .outerjoin(WrongBookEntry, WrongBookEntry.question_id == QuestionCandidate.id)
        .where(ready_candidate_predicate())
        .options(selectinload(QuestionCandidate.image_parts))
    ).all()
    image_ids = {
        image_id
        for row in rows
        for image_id in (
            [row[0].source_image_id]
            + [part.source_image_id for part in row[0].image_parts]
        )
    }
    image_orders = dict(
        session.execute(
            select(SourceImage.id, SourceImage.order_index).where(
                SourceImage.id.in_(image_ids)
            )
        ).all()
    ) if image_ids else {}
    answered_ids = {
        question_id
        for question_id in session.scalars(
            select(PracticeItem.question_id)
            .join(Attempt, Attempt.practice_item_id == PracticeItem.id)
            .where(
                PracticeItem.question_id.is_not(None),
                Attempt.was_unanswered.is_(False),
            )
        )
        if question_id is not None
    }
    ready: list[_ReadyRow] = []
    for (
        question,
        course_id,
        course_name,
        category_id,
        category_path,
        batch_id,
        batch_name,
        batch_created_at,
        wrong_id,
        mastered_at,
    ) in rows:
        first_part = min(question.image_parts, key=lambda part: part.part_index, default=None)
        source_image_id = first_part.source_image_id if first_part else question.source_image_id
        source_y_start = first_part.source_y_start if first_part else 0
        ready.append(
            _ReadyRow(
                question=question,
                course_id=course_id,
                course_name=course_name,
                category_id=category_id,
                category_path=category_path or "未分类",
                batch_id=batch_id,
                batch_name=batch_name,
                batch_created_at_key=batch_created_at.isoformat(),
                in_wrong_book=wrong_id is not None and mastered_at is None,
                has_answered=question.id in answered_ids,
                source_order=image_orders.get(source_image_id, 2**31 - 1),
                source_y_start=source_y_start,
            )
        )
    return ready


def _with_positions(rows: list[_ReadyRow]) -> list[SelectedQuestion]:
    by_batch: dict[str, list[_ReadyRow]] = {}
    for row in rows:
        by_batch.setdefault(row.batch_id, []).append(row)
    positions: dict[str, int] = {}
    for batch_rows in by_batch.values():
        ordered = sorted(
            batch_rows,
            key=lambda row: (
                row.source_order,
                row.source_y_start,
                row.question.created_at.isoformat(),
                row.question.id,
            ),
        )
        positions.update(
            {row.question.id: index for index, row in enumerate(ordered, start=1)}
        )
    selected = [
        SelectedQuestion(
            question=row.question,
            course_id=row.course_id,
            course_name=row.course_name,
            category_id=row.category_id,
            category_path=row.category_path,
            batch_id=row.batch_id,
            batch_name=row.batch_name,
            batch_position=positions[row.question.id],
            in_wrong_book=row.in_wrong_book,
            has_answered=row.has_answered,
            batch_created_at_key=row.batch_created_at_key,
        )
        for row in rows
    ]
    return sorted(
        selected,
        key=lambda item: (
            item.course_name,
            item.category_path,
            item.batch_created_at_key,
            item.batch_id,
            item.batch_position,
            item.question.id,
        ),
    )


def list_practice_candidates(
    session: Session, criteria: SelectionCriteria
) -> list[SelectedQuestion]:
    _validate(criteria)
    all_ready = _with_positions(_ready_rows(session))
    requested_ids = set(criteria.manual_question_ids)
    if requested_ids:
        available_ids = {item.question.id for item in all_ready}
        if not requested_ids <= available_ids:
            raise PracticeSelectionError("手动选择的题目不全在正式题库中")

    result: list[SelectedQuestion] = []
    for item in all_ready:
        if criteria.course_id and item.course_id != criteria.course_id:
            continue
        if criteria.category_id and item.category_id != criteria.category_id:
            continue
        if criteria.batch_id and item.batch_id != criteria.batch_id:
            continue
        if criteria.question_type and item.question.question_type != criteria.question_type:
            continue
        if criteria.membership == "wrong" and not item.in_wrong_book:
            continue
        if criteria.membership == "unanswered" and item.has_answered:
            continue
        if requested_ids and item.question.id not in requested_ids:
            continue
        if criteria.position_start is not None:
            assert criteria.position_end is not None
            if not criteria.position_start <= item.batch_position <= criteria.position_end:
                continue
        result.append(item)

    if requested_ids and {item.question.id for item in result} != requested_ids:
        raise PracticeSelectionError("手动选择的题目不满足当前正式题库筛选条件")
    return result


def select_questions(
    session: Session,
    criteria: SelectionCriteria,
    count: int | None,
    order_mode: Literal["sequential", "random"],
    *,
    rng: ShuffleSource | None = None,
) -> tuple[list[SelectedQuestion], bool]:
    if count is not None and count < 1:
        raise PracticeSelectionError("选题数量必须大于零")
    if order_mode not in {"sequential", "random"}:
        raise PracticeSelectionError("出题顺序无效")
    available = list_practice_candidates(session, criteria)
    if not available:
        raise PracticeSelectionError("当前条件下没有可用的正式题目")
    ordered = list(available)
    if order_mode == "random":
        (rng or random.SystemRandom()).shuffle(ordered)
    if count is None:
        return ordered, False
    adjusted = count > len(ordered)
    return ordered[:count], adjusted
