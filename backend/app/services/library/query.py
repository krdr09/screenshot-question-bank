from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import Category, Course, ImportBatch, QuestionCandidate, WrongBookEntry
from app.services.candidate_eligibility import ready_candidate_predicate


@dataclass(frozen=True)
class LibraryFilters:
    course_id: str | None = None
    category_id: str | None = None
    batch_id: str | None = None
    question_type: str | None = None
    search: str | None = None
    page: int = 1
    page_size: int = 50


@dataclass(frozen=True)
class LibraryQuestion:
    candidate: QuestionCandidate
    course_id: str
    course_name: str
    category_id: str | None
    category_path: str
    batch_id: str
    batch_name: str
    in_wrong_book: bool


@dataclass(frozen=True)
class LibraryPage:
    items: list[LibraryQuestion]
    total: int
    page: int
    page_size: int


@dataclass(frozen=True)
class CategoryMetadata:
    id: str
    path: str


@dataclass(frozen=True)
class CourseMetadata:
    id: str
    name: str
    categories: list[CategoryMetadata]


@dataclass(frozen=True)
class BatchMetadata:
    id: str
    name: str
    status: str
    course_id: str
    course_name: str
    category_id: str | None
    category_path: str
    question_count: int


def _effective_category():
    return func.coalesce(QuestionCandidate.category_id, ImportBatch.category_id)


def _base_query() -> Select:
    return (
        select(
            QuestionCandidate,
            Course.id,
            Course.name,
            Category.id,
            Category.path,
            ImportBatch.id,
            ImportBatch.name,
            WrongBookEntry.id,
        )
        .join(ImportBatch, ImportBatch.id == QuestionCandidate.batch_id)
        .join(Course, Course.id == ImportBatch.course_id)
        .outerjoin(Category, Category.id == _effective_category())
        .outerjoin(WrongBookEntry, WrongBookEntry.question_id == QuestionCandidate.id)
        .where(ready_candidate_predicate())
    )


def _apply_filters(statement: Select, filters: LibraryFilters) -> Select:
    if filters.course_id:
        statement = statement.where(Course.id == filters.course_id)
    if filters.category_id:
        statement = statement.where(_effective_category() == filters.category_id)
    if filters.batch_id:
        statement = statement.where(ImportBatch.id == filters.batch_id)
    if filters.question_type:
        statement = statement.where(QuestionCandidate.question_type == filters.question_type)
    if filters.search and filters.search.strip():
        pattern = f"%{filters.search.strip()}%"
        statement = statement.where(
            or_(
                QuestionCandidate.raw_ocr_text.ilike(pattern),
                QuestionCandidate.edited_ocr_text.ilike(pattern),
                QuestionCandidate.normalized_ocr_text.ilike(pattern),
                QuestionCandidate.stem.ilike(pattern),
            )
        )
    return statement


def _row(row) -> LibraryQuestion:
    candidate, course_id, course_name, category_id, category_path, batch_id, batch_name, wrong_id = row
    return LibraryQuestion(
        candidate=candidate,
        course_id=course_id,
        course_name=course_name,
        category_id=category_id,
        category_path=category_path or "未分类",
        batch_id=batch_id,
        batch_name=batch_name,
        in_wrong_book=wrong_id is not None,
    )


def list_questions(session: Session, filters: LibraryFilters) -> LibraryPage:
    page = max(filters.page, 1)
    page_size = min(max(filters.page_size, 1), 200)
    filtered = _apply_filters(_base_query(), filters)
    total = int(session.scalar(select(func.count()).select_from(filtered.subquery())) or 0)
    rows = session.execute(
        filtered
        .options(selectinload(QuestionCandidate.image_parts))
        .order_by(QuestionCandidate.created_at, QuestionCandidate.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return LibraryPage([_row(row) for row in rows], total, page, page_size)


def get_question(
    session: Session,
    question_id: str,
    *,
    course_id: str | None = None,
) -> LibraryQuestion | None:
    filters = LibraryFilters(course_id=course_id)
    statement = _apply_filters(_base_query(), filters).where(QuestionCandidate.id == question_id)
    row = session.execute(
        statement.options(selectinload(QuestionCandidate.image_parts))
    ).one_or_none()
    return _row(row) if row else None


def list_library_metadata(
    session: Session,
    *,
    course_id: str | None = None,
    category_id: str | None = None,
) -> tuple[list[CourseMetadata], list[BatchMetadata]]:
    courses = list(session.scalars(select(Course).order_by(Course.name, Course.id)))
    categories = list(session.scalars(select(Category).order_by(Category.path, Category.id)))
    categories_by_course: dict[str, list[CategoryMetadata]] = {}
    for category in categories:
        categories_by_course.setdefault(category.course_id, []).append(
            CategoryMetadata(category.id, category.path)
        )
    course_items = [
        CourseMetadata(course.id, course.name, categories_by_course.get(course.id, []))
        for course in courses
    ]
    count_statement = (
        select(
            QuestionCandidate.batch_id.label("batch_id"),
            func.count(QuestionCandidate.id).label("question_count"),
        )
        .join(ImportBatch, ImportBatch.id == QuestionCandidate.batch_id)
        .where(ready_candidate_predicate())
    )
    if category_id:
        count_statement = count_statement.where(_effective_category() == category_id)
    count_subquery = count_statement.group_by(QuestionCandidate.batch_id).subquery()
    statement = (
        select(
            ImportBatch,
            Course.name,
            Category.path,
            func.coalesce(count_subquery.c.question_count, 0),
        )
        .join(Course, Course.id == ImportBatch.course_id)
        .outerjoin(Category, Category.id == ImportBatch.category_id)
        .outerjoin(count_subquery, count_subquery.c.batch_id == ImportBatch.id)
        .order_by(ImportBatch.created_at.desc(), ImportBatch.id)
    )
    if course_id:
        statement = statement.where(ImportBatch.course_id == course_id)
    if category_id:
        statement = statement.where(count_subquery.c.batch_id.is_not(None))
    batch_items = [
        BatchMetadata(
            batch.id,
            batch.name,
            batch.status,
            batch.course_id,
            course_name,
            batch.category_id,
            category_path or "未分类",
            int(question_count),
        )
        for batch, course_name, category_path, question_count in session.execute(statement)
    ]
    return course_items, batch_items
