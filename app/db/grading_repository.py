from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.dto import ExamCreate
from app.model import Attempt, Course, Exam, Question, QuestionGrade

grading_metadata = MetaData()

courses = Table(
    "grading_courses",
    grading_metadata,
    Column("id", String(128), primary_key=True),
    Column("course_code", String(128), nullable=False),
    Column("title", String(300), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

exams = Table(
    "grading_exams",
    grading_metadata,
    Column("id", String(128), primary_key=True),
    Column("course_id", ForeignKey("grading_courses.id"), nullable=False, index=True),
    Column("title", String(300), nullable=False),
    Column("type", String(16), nullable=False),
    Column("max_attempts", Integer, nullable=False),
    Column("rubric_id", String(128), nullable=True, unique=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

questions = Table(
    "grading_questions",
    grading_metadata,
    # id is only unique *within an exam* (matching the instructor-supplied CSV
    # question_id), not globally — two different exams may both have a "Q001".
    Column("id", String(128), nullable=False),
    Column("exam_id", ForeignKey("grading_exams.id"), nullable=False, index=True),
    Column("position", Integer, nullable=False),
    Column("prompt", Text, nullable=False),
    Column("max_score", Float, nullable=False),
    Column("criteria", JSON, nullable=False),
    Column("rubric_chunk_indexes", JSON, nullable=False),
    PrimaryKeyConstraint("exam_id", "id", name="pk_grading_questions"),
    UniqueConstraint("exam_id", "position", name="uq_grading_question_position"),
)

attempts = Table(
    "grading_attempts",
    grading_metadata,
    Column("id", String(36), primary_key=True),
    Column("exam_id", ForeignKey("grading_exams.id"), nullable=False, index=True),
    Column("student_id", String(128), nullable=False, index=True),
    Column("attempt_number", Integer, nullable=False),
    Column("status", String(32), nullable=False),
    Column("rubric_id", String(128), nullable=False),
    Column("rubric_version", String(64), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("graded_at", DateTime(timezone=True), nullable=True),
    Column("error", Text, nullable=True),
    UniqueConstraint(
        "exam_id",
        "student_id",
        "attempt_number",
        name="uq_grading_student_exam_attempt",
    ),
)

responses = Table(
    "grading_responses",
    grading_metadata,
    Column("id", String(36), primary_key=True),
    Column("attempt_id", ForeignKey("grading_attempts.id"), nullable=False, index=True),
    Column("exam_id", ForeignKey("grading_exams.id"), nullable=False, index=True),
    Column("question_id", String(128), nullable=False),
    Column("answer", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["exam_id", "question_id"],
        ["grading_questions.exam_id", "grading_questions.id"],
    ),
    UniqueConstraint("attempt_id", "question_id", name="uq_grading_attempt_response"),
)

grades = Table(
    "grading_question_grades",
    grading_metadata,
    Column("id", String(36), primary_key=True),
    Column("attempt_id", ForeignKey("grading_attempts.id"), nullable=False, index=True),
    Column("response_id", ForeignKey("grading_responses.id"), nullable=False),
    Column("exam_id", ForeignKey("grading_exams.id"), nullable=False, index=True),
    Column("question_id", String(128), nullable=False),
    Column("score", Float, nullable=False),
    Column("max_score", Float, nullable=False),
    Column("feedback", Text, nullable=False),
    Column("criteria", JSON, nullable=False),
    Column("rubric_id", String(128), nullable=False),
    Column("rubric_version", String(64), nullable=False),
    Column("rubric_chunk_ids", JSON, nullable=False),
    Column("llm_model", String(255), nullable=False),
    Column("prompt_version", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["exam_id", "question_id"],
        ["grading_questions.exam_id", "grading_questions.id"],
    ),
    UniqueConstraint("attempt_id", "question_id", name="uq_grading_attempt_grade"),
)


class GradingStoreError(RuntimeError):
    pass


class GradingRecordNotFoundError(KeyError):
    pass


class GradingConflictError(ValueError):
    pass


class AttemptLimitExceededError(ValueError):
    pass


class AttemptStateError(ValueError):
    pass


class PostgresGradingRepository:
    def __init__(
        self,
        database_url: str | None = None,
        *,
        engine: Engine | None = None,
    ) -> None:
        if engine is None and database_url is None:
            raise ValueError("database_url is required when engine is not provided.")
        self.engine = engine or create_engine(database_url, pool_pre_ping=True)

    def initialize(self) -> None:
        grading_metadata.create_all(self.engine)

    def close(self) -> None:
        self.engine.dispose()

    def health(self) -> bool:
        try:
            with self.engine.connect() as connection:
                connection.execute(select(func.count()).select_from(courses))
            return True
        except SQLAlchemyError:
            return False

    def create_course(self, course_code: str, title: str) -> Course:
        now = datetime.now(UTC)
        try:
            with self.engine.begin() as connection:
                # Course ids are auto-incremented, one higher than the current max
                # *numeric* id. Computed in Python rather than SQL CAST so that
                # any pre-existing non-numeric ids (e.g. from before this change)
                # are simply ignored instead of raising a DB-level cast error.
                existing_ids = connection.execute(select(courses.c.id)).scalars().all()
                numeric_ids = [int(value) for value in existing_ids if value.isdigit()]
                course_id = str(max(numeric_ids, default=0) + 1)
                connection.execute(
                    insert(courses).values(
                        id=course_id,
                        course_code=course_code,
                        title=title,
                        created_at=now,
                    )
                )
        except IntegrityError as exc:
            raise GradingConflictError(f"Course '{course_id}' already exists.") from exc
        return Course(
            id=course_id,
            course_code=course_code,
            title=title,
            created_at=now,
        )

    def get_course(self, course_id: str) -> Course:
        with self.engine.connect() as connection:
            row = (
                connection.execute(select(courses).where(courses.c.id == course_id))
                .mappings()
                .first()
            )
        if row is None:
            raise GradingRecordNotFoundError(course_id)
        return Course.model_validate(dict(row))

    def list_courses(self) -> list[Course]:
        with self.engine.connect() as connection:
            rows = (
                connection.execute(select(courses).order_by(courses.c.created_at))
                .mappings()
                .all()
            )
        return [Course.model_validate(dict(row)) for row in rows]

    def create_exam(self, course_id: str, request: ExamCreate) -> Exam:
        now = datetime.now(UTC)
        exam_id = str(uuid.uuid4())
        try:
            with self.engine.begin() as connection:
                if not self._course_exists(connection, course_id):
                    raise GradingRecordNotFoundError(course_id)
                connection.execute(
                    insert(exams).values(
                        id=exam_id,
                        course_id=course_id,
                        title=request.title,
                        type=request.type,
                        max_attempts=request.max_attempts,
                        rubric_id=request.rubric_id,
                        created_at=now,
                    )
                )
                connection.execute(
                    insert(questions),
                    [
                        {
                            "id": question.id,
                            "exam_id": exam_id,
                            "position": position,
                            "prompt": question.prompt,
                            "max_score": question.max_score,
                            "criteria": question.criteria,
                            "rubric_chunk_indexes": question.rubric_chunk_indexes,
                        }
                        for position, question in enumerate(request.questions)
                    ],
                )
        except IntegrityError as exc:
            raise GradingConflictError(
                "Exam, rubric, or question identifiers already exist."
            ) from exc
        return self.get_exam(exam_id)

    def get_exam(self, exam_id: str) -> Exam:
        try:
            with self.engine.connect() as connection:
                exam_row = (
                    connection.execute(select(exams).where(exams.c.id == exam_id))
                    .mappings()
                    .first()
                )
                question_rows = (
                    connection.execute(
                        select(questions)
                        .where(questions.c.exam_id == exam_id)
                        .order_by(questions.c.position)
                    )
                    .mappings()
                    .all()
                )
        except SQLAlchemyError as exc:
            raise GradingStoreError("Unable to read exam data.") from exc
        if exam_row is None:
            raise GradingRecordNotFoundError(exam_id)
        return self._to_exam(exam_row, question_rows)

    def list_exams(self, course_id: str) -> list[Exam]:
        if not self._course_exists_for_read(course_id):
            raise GradingRecordNotFoundError(course_id)
        with self.engine.connect() as connection:
            ids = (
                connection.execute(
                    select(exams.c.id)
                    .where(exams.c.course_id == course_id)
                    .order_by(exams.c.created_at)
                )
                .scalars()
                .all()
            )
        return [self.get_exam(exam_id) for exam_id in ids]

    def update_exam_rubric(self, exam_id: str, rubric_id: str) -> Exam:
        try:
            with self.engine.begin() as connection:
                result = connection.execute(
                    update(exams)
                    .where(exams.c.id == exam_id)
                    .values(rubric_id=rubric_id)
                )
        except IntegrityError as exc:
            raise GradingConflictError(
                f"Rubric '{rubric_id}' is already assigned to another exam."
            ) from exc
        if result.rowcount == 0:
            raise GradingRecordNotFoundError(exam_id)
        return self.get_exam(exam_id)

    def update_question_chunk_indexes(
        self,
        exam_id: str,
        question_id: str,
        chunk_indexes: list[int],
    ) -> Question:
        statement = (
            update(questions)
            .where(questions.c.id == question_id, questions.c.exam_id == exam_id)
            .values(rubric_chunk_indexes=chunk_indexes)
        )
        with self.engine.begin() as connection:
            result = connection.execute(statement)
        if result.rowcount == 0:
            raise GradingRecordNotFoundError(question_id)
        return next(
            question
            for question in self.get_exam(exam_id).questions
            if question.id == question_id
        )

    def create_attempt(
        self,
        *,
        exam_id: str,
        student_id: str,
        rubric_id: str,
        rubric_version: str,
    ) -> Attempt:
        now = datetime.now(UTC)
        attempt_id = str(uuid.uuid4())
        try:
            with self.engine.begin() as connection:
                exam_row = (
                    connection.execute(
                        select(exams).where(exams.c.id == exam_id).with_for_update()
                    )
                    .mappings()
                    .first()
                )
                if exam_row is None:
                    raise GradingRecordNotFoundError(exam_id)
                used = connection.execute(
                    select(func.count())
                    .select_from(attempts)
                    .where(
                        attempts.c.exam_id == exam_id,
                        attempts.c.student_id == student_id,
                    )
                ).scalar_one()
                if used >= exam_row["max_attempts"]:
                    raise AttemptLimitExceededError(
                        f"Exam '{exam_id}' allows {exam_row['max_attempts']} attempt(s)."
                    )
                attempt_number = used + 1
                connection.execute(
                    insert(attempts).values(
                        id=attempt_id,
                        exam_id=exam_id,
                        student_id=student_id,
                        attempt_number=attempt_number,
                        status="in_progress",
                        rubric_id=rubric_id,
                        rubric_version=rubric_version,
                        started_at=now,
                        graded_at=None,
                        error=None,
                    )
                )
        except IntegrityError as exc:
            raise GradingConflictError(
                "Concurrent attempt creation conflicted."
            ) from exc
        return self.get_attempt(attempt_id)

    def get_attempt(self, attempt_id: str) -> Attempt:
        try:
            with self.engine.connect() as connection:
                row = (
                    connection.execute(
                        select(attempts).where(attempts.c.id == attempt_id)
                    )
                    .mappings()
                    .first()
                )
        except SQLAlchemyError as exc:
            raise GradingStoreError("Unable to read attempt data.") from exc
        if row is None:
            raise GradingRecordNotFoundError(attempt_id)
        return Attempt.model_validate(dict(row))

    def list_attempts(self, exam_id: str, student_id: str) -> list[Attempt]:
        statement = (
            select(attempts)
            .where(
                attempts.c.exam_id == exam_id,
                attempts.c.student_id == student_id,
            )
            .order_by(attempts.c.attempt_number)
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [Attempt.model_validate(dict(row)) for row in rows]

    def save_response(
        self, attempt_id: str, exam_id: str, question_id: str, answer: str
    ) -> str:
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            row = connection.execute(
                select(responses.c.id).where(
                    responses.c.attempt_id == attempt_id,
                    responses.c.question_id == question_id,
                )
            ).first()
            if row is None:
                response_id = str(uuid.uuid4())
                connection.execute(
                    insert(responses).values(
                        id=response_id,
                        attempt_id=attempt_id,
                        exam_id=exam_id,
                        question_id=question_id,
                        answer=answer,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                response_id = str(row[0])
                connection.execute(
                    update(responses)
                    .where(responses.c.id == response_id)
                    .values(answer=answer, updated_at=now)
                )
        return response_id

    def save_grade(
        self,
        *,
        attempt_id: str,
        exam_id: str,
        response_id: str,
        question_id: str,
        score: float,
        max_score: float,
        feedback: str,
        criteria: list[dict],
        rubric_id: str,
        rubric_version: str,
        rubric_chunk_ids: list[str],
        llm_model: str,
        prompt_version: str,
    ) -> None:
        now = datetime.now(UTC)
        values = {
            "response_id": response_id,
            "score": score,
            "max_score": max_score,
            "feedback": feedback,
            "criteria": criteria,
            "rubric_id": rubric_id,
            "rubric_version": rubric_version,
            "rubric_chunk_ids": rubric_chunk_ids,
            "llm_model": llm_model,
            "prompt_version": prompt_version,
            "created_at": now,
        }
        with self.engine.begin() as connection:
            row = connection.execute(
                select(grades.c.id).where(
                    grades.c.attempt_id == attempt_id,
                    grades.c.question_id == question_id,
                )
            ).first()
            if row is None:
                connection.execute(
                    insert(grades).values(
                        id=str(uuid.uuid4()),
                        attempt_id=attempt_id,
                        exam_id=exam_id,
                        question_id=question_id,
                        **values,
                    )
                )
            else:
                connection.execute(
                    update(grades).where(grades.c.id == row[0]).values(**values)
                )

    def list_grades(self, attempt_id: str) -> list[QuestionGrade]:
        statement = (
            select(grades, questions.c.position)
            .join(
                questions,
                (questions.c.id == grades.c.question_id)
                & (questions.c.exam_id == grades.c.exam_id),
            )
            .where(grades.c.attempt_id == attempt_id)
            .order_by(questions.c.position)
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [
            QuestionGrade(
                question_id=row["question_id"],
                score=row["score"],
                max_score=row["max_score"],
                percentage=round(row["score"] / row["max_score"] * 100, 2),
                feedback=row["feedback"],
                criteria=row["criteria"],
                rubric_chunk_ids=row["rubric_chunk_ids"],
            )
            for row in rows
        ]

    def mark_attempt_graded(self, attempt_id: str) -> Attempt:
        with self.engine.begin() as connection:
            result = connection.execute(
                update(attempts)
                .where(attempts.c.id == attempt_id)
                .values(
                    status="graded",
                    graded_at=datetime.now(UTC),
                    error=None,
                )
            )
        if result.rowcount == 0:
            raise GradingRecordNotFoundError(attempt_id)
        return self.get_attempt(attempt_id)

    def mark_attempt_in_progress(self, attempt_id: str) -> None:
        with self.engine.begin() as connection:
            result = connection.execute(
                update(attempts)
                .where(attempts.c.id == attempt_id)
                .values(status="in_progress", error=None)
            )
        if result.rowcount == 0:
            raise GradingRecordNotFoundError(attempt_id)

    def mark_attempt_failed(self, attempt_id: str, error: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                update(attempts)
                .where(attempts.c.id == attempt_id)
                .values(status="failed", error=error[:2000])
            )

    @staticmethod
    def _course_exists(connection: Connection, course_id: str) -> bool:
        return (
            connection.execute(
                select(courses.c.id).where(courses.c.id == course_id).limit(1)
            ).first()
            is not None
        )

    def _course_exists_for_read(self, course_id: str) -> bool:
        with self.engine.connect() as connection:
            return self._course_exists(connection, course_id)

    @staticmethod
    def _to_exam(exam_row: RowMapping, question_rows: list[RowMapping]) -> Exam:
        return Exam(
            **dict(exam_row),
            questions=[Question.model_validate(dict(row)) for row in question_rows],
        )
