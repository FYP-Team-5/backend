from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    delete,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.dto import RubricCreate, TestCreate
from app.model import (
    Attempt,
    Course,
    Criteria,
    CriteriaMet,
    Question,
    Response,
    Rubric,
    Test,
)

grading_metadata = MetaData()

courses = Table(
    "grading_courses",
    grading_metadata,
    Column("id", String(128), primary_key=True),
    Column("course_code", String(128), nullable=False),
    Column("course_name", String(300), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

tests = Table(
    "grading_tests",
    grading_metadata,
    Column("id", String(128), primary_key=True),
    Column("course_id", ForeignKey("grading_courses.id"), nullable=False, index=True),
    Column("test_name", String(300), nullable=False),
    Column("max_attempts", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

rubrics = Table(
    "grading_rubrics",
    grading_metadata,
    Column("id", String(128), primary_key=True),
)

criteria = Table(
    "grading_criteria",
    grading_metadata,
    Column("id", String(128), primary_key=True),
    Column("rubric_id", ForeignKey("grading_rubrics.id"), nullable=False, index=True),
    Column("description", Text, nullable=False),
    Column("score", Float, nullable=False),
)

questions = Table(
    "grading_questions",
    grading_metadata,
    Column("id", String(128), primary_key=True),
    Column("test_id", ForeignKey("grading_tests.id"), nullable=False, index=True),
    Column("position", Integer, nullable=False),
    Column("prompt", Text, nullable=False),
    Column("max_score", Float, nullable=False),
    Column("score_increment", Float, nullable=False),
    Column("rubric_id", ForeignKey("grading_rubrics.id"), nullable=True, unique=True),
    UniqueConstraint("test_id", "position", name="uq_grading_question_position"),
)

attempts = Table(
    "grading_attempts",
    grading_metadata,
    Column("id", String(36), primary_key=True),
    Column("test_id", ForeignKey("grading_tests.id"), nullable=False, index=True),
    Column("user_id", String(128), nullable=False, index=True),
    Column("attempt_number", Integer, nullable=False),
    Column("status", String(32), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("graded_at", DateTime(timezone=True), nullable=True),
    Column("error", Text, nullable=True),
    UniqueConstraint(
        "test_id",
        "user_id",
        "attempt_number",
        name="uq_grading_user_test_attempt",
    ),
)

responses = Table(
    "grading_responses",
    grading_metadata,
    Column("id", String(36), primary_key=True),
    Column("attempt_id", ForeignKey("grading_attempts.id"), nullable=False, index=True),
    Column("question_id", ForeignKey("grading_questions.id"), nullable=False, index=True),
    Column("answer", Text, nullable=False),
    Column("score", Float, nullable=True),
    Column("feedback", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("attempt_id", "question_id", name="uq_grading_attempt_response"),
)

criteria_met = Table(
    "grading_criteria_met",
    grading_metadata,
    Column("id", String(36), primary_key=True),
    Column("response_id", ForeignKey("grading_responses.id"), nullable=False, index=True),
    Column("criteria_id", ForeignKey("grading_criteria.id"), nullable=False, index=True),
    Column("is_met", Boolean, nullable=False),
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

    def create_course(self, course_code: str, course_name: str) -> Course:
        now = datetime.now(UTC)
        try:
            with self.engine.begin() as connection:
                existing_ids = connection.execute(select(courses.c.id)).scalars().all()
                numeric_ids = [int(value) for value in existing_ids if value.isdigit()]
                course_id = str(max(numeric_ids, default=0) + 1)
                connection.execute(
                    insert(courses).values(
                        id=course_id,
                        course_code=course_code,
                        course_name=course_name,
                        created_at=now,
                    )
                )
        except IntegrityError as exc:
            raise GradingConflictError(f"Course '{course_id}' already exists.") from exc
        return Course(
            id=course_id,
            course_code=course_code,
            course_name=course_name,
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

    def create_test(self, course_id: str, request: TestCreate) -> Test:
        now = datetime.now(UTC)
        test_id = str(uuid.uuid4())
        question_rubric_ids = [
            str(uuid.uuid4()) if question.rubric is not None else None
            for question in request.questions
        ]
        rubric_ids_to_create = [
            rubric_id for rubric_id in question_rubric_ids if rubric_id is not None
        ]
        try:
            with self.engine.begin() as connection:
                if not self._course_exists(connection, course_id):
                    raise GradingRecordNotFoundError(course_id)
                connection.execute(
                    insert(tests).values(
                        id=test_id,
                        course_id=course_id,
                        test_name=request.test_name,
                        max_attempts=request.max_attempts,
                        created_at=now,
                    )
                )
                if rubric_ids_to_create:
                    connection.execute(
                        insert(rubrics),
                        [{"id": rubric_id} for rubric_id in rubric_ids_to_create],
                    )
                criteria_rows = [
                    {
                        "id": str(uuid.uuid4()),
                        "rubric_id": rubric_id,
                        "description": item.description,
                        "score": item.score,
                    }
                    for rubric_id, question in zip(question_rubric_ids, request.questions)
                    if rubric_id is not None
                    for item in question.rubric.criteria
                ]
                if criteria_rows:
                    connection.execute(insert(criteria), criteria_rows)
                connection.execute(
                    insert(questions),
                    [
                        {
                            "id": str(uuid.uuid4()),
                            "test_id": test_id,
                            "position": position,
                            "prompt": question.prompt,
                            "max_score": question.max_score,
                            "score_increment": question.score_increment,
                            "rubric_id": rubric_id,
                        }
                        for position, (rubric_id, question) in enumerate(
                            zip(question_rubric_ids, request.questions)
                        )
                    ],
                )
        except IntegrityError as exc:
            raise GradingConflictError("Test identifiers already exist.") from exc
        return self.get_test(test_id)

    def set_question_rubric(
        self, test_id: str, question_id: str, request: RubricCreate
    ) -> Question:
        new_rubric_id = str(uuid.uuid4())
        try:
            with self.engine.begin() as connection:
                question_row = (
                    connection.execute(
                        select(questions)
                        .where(questions.c.id == question_id, questions.c.test_id == test_id)
                        .with_for_update()
                    )
                    .mappings()
                    .first()
                )
                if question_row is None:
                    raise GradingRecordNotFoundError(question_id)
                old_rubric_id = question_row["rubric_id"]
                connection.execute(insert(rubrics).values(id=new_rubric_id))
                connection.execute(
                    insert(criteria),
                    [
                        {
                            "id": str(uuid.uuid4()),
                            "rubric_id": new_rubric_id,
                            "description": item.description,
                            "score": item.score,
                        }
                        for item in request.criteria
                    ],
                )
                connection.execute(
                    update(questions)
                    .where(questions.c.id == question_id)
                    .values(rubric_id=new_rubric_id)
                )
                if old_rubric_id is not None:
                    connection.execute(
                        delete(criteria).where(criteria.c.rubric_id == old_rubric_id)
                    )
                    connection.execute(delete(rubrics).where(rubrics.c.id == old_rubric_id))
        except IntegrityError as exc:
            raise GradingConflictError("Rubric identifiers already exist.") from exc
        return self._get_question(question_id)

    def get_test(self, test_id: str) -> Test:
        try:
            with self.engine.connect() as connection:
                test_row = (
                    connection.execute(select(tests).where(tests.c.id == test_id))
                    .mappings()
                    .first()
                )
                question_rows = (
                    connection.execute(
                        select(questions)
                        .where(questions.c.test_id == test_id)
                        .order_by(questions.c.position)
                    )
                    .mappings()
                    .all()
                )
                rubric_ids = [
                    row["rubric_id"] for row in question_rows if row["rubric_id"] is not None
                ]
                criteria_rows = (
                    connection.execute(
                        select(criteria).where(criteria.c.rubric_id.in_(rubric_ids))
                    )
                    .mappings()
                    .all()
                    if rubric_ids
                    else []
                )
        except SQLAlchemyError as exc:
            raise GradingStoreError("Unable to read test data.") from exc
        if test_row is None:
            raise GradingRecordNotFoundError(test_id)
        criteria_by_rubric: dict[str, list[RowMapping]] = {}
        for row in criteria_rows:
            criteria_by_rubric.setdefault(row["rubric_id"], []).append(row)
        return self._to_test(test_row, question_rows, criteria_by_rubric)

    def list_tests(self, course_id: str) -> list[Test]:
        if not self._course_exists_for_read(course_id):
            raise GradingRecordNotFoundError(course_id)
        with self.engine.connect() as connection:
            ids = (
                connection.execute(
                    select(tests.c.id)
                    .where(tests.c.course_id == course_id)
                    .order_by(tests.c.created_at)
                )
                .scalars()
                .all()
            )
        return [self.get_test(test_id) for test_id in ids]

    def create_attempt(self, *, test_id: str, user_id: str) -> Attempt:
        now = datetime.now(UTC)
        attempt_id = str(uuid.uuid4())
        try:
            with self.engine.begin() as connection:
                test_row = (
                    connection.execute(
                        select(tests).where(tests.c.id == test_id).with_for_update()
                    )
                    .mappings()
                    .first()
                )
                if test_row is None:
                    raise GradingRecordNotFoundError(test_id)
                used = connection.execute(
                    select(func.count())
                    .select_from(attempts)
                    .where(
                        attempts.c.test_id == test_id,
                        attempts.c.user_id == user_id,
                    )
                ).scalar_one()
                if used >= test_row["max_attempts"]:
                    raise AttemptLimitExceededError(
                        f"Test '{test_id}' allows {test_row['max_attempts']} attempt(s)."
                    )
                attempt_number = used + 1
                connection.execute(
                    insert(attempts).values(
                        id=attempt_id,
                        test_id=test_id,
                        user_id=user_id,
                        attempt_number=attempt_number,
                        status="in_progress",
                        started_at=now,
                        graded_at=None,
                        error=None,
                    )
                )
        except IntegrityError as exc:
            raise GradingConflictError("Concurrent attempt creation conflicted.") from exc
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

    def list_attempts(self, test_id: str, user_id: str) -> list[Attempt]:
        statement = (
            select(attempts)
            .where(
                attempts.c.test_id == test_id,
                attempts.c.user_id == user_id,
            )
            .order_by(attempts.c.attempt_number)
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [Attempt.model_validate(dict(row)) for row in rows]

    def save_response(self, attempt_id: str, question_id: str, answer: str) -> str:
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
                        question_id=question_id,
                        answer=answer,
                        score=None,
                        feedback=None,
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

    def save_response_grade(
        self,
        *,
        response_id: str,
        score: float,
        feedback: str,
        criteria_met_results: list[dict],
    ) -> None:
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            result = connection.execute(
                update(responses)
                .where(responses.c.id == response_id)
                .values(score=score, feedback=feedback, updated_at=now)
            )
            if result.rowcount == 0:
                raise GradingRecordNotFoundError(response_id)
            connection.execute(
                delete(criteria_met).where(criteria_met.c.response_id == response_id)
            )
            connection.execute(
                insert(criteria_met),
                [
                    {
                        "id": str(uuid.uuid4()),
                        "response_id": response_id,
                        "criteria_id": item["criteria_id"],
                        "is_met": item["is_met"],
                    }
                    for item in criteria_met_results
                ],
            )

    def list_responses(self, attempt_id: str) -> list[Response]:
        statement = (
            select(responses, questions.c.position)
            .join(questions, questions.c.id == responses.c.question_id)
            .where(responses.c.attempt_id == attempt_id, responses.c.score.isnot(None))
            .order_by(questions.c.position)
        )
        with self.engine.connect() as connection:
            response_rows = connection.execute(statement).mappings().all()
            response_ids = [row["id"] for row in response_rows]
            criteria_met_rows = (
                connection.execute(
                    select(criteria_met).where(criteria_met.c.response_id.in_(response_ids))
                )
                .mappings()
                .all()
                if response_ids
                else []
            )
        grouped: dict[str, list[RowMapping]] = {}
        for row in criteria_met_rows:
            grouped.setdefault(row["response_id"], []).append(row)
        return [
            self._to_response(row, grouped.get(row["id"], []))
            for row in response_rows
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

    def _get_question(self, question_id: str) -> Question:
        with self.engine.connect() as connection:
            question_row = (
                connection.execute(select(questions).where(questions.c.id == question_id))
                .mappings()
                .first()
            )
            if question_row is None:
                raise GradingRecordNotFoundError(question_id)
            criteria_rows = []
            if question_row["rubric_id"] is not None:
                criteria_rows = (
                    connection.execute(
                        select(criteria).where(criteria.c.rubric_id == question_row["rubric_id"])
                    )
                    .mappings()
                    .all()
                )
        return self._to_question(question_row, criteria_rows)

    @staticmethod
    def _to_question(question_row: RowMapping, criteria_rows: list[RowMapping]) -> Question:
        rubric_id = question_row["rubric_id"]
        rubric = (
            Rubric(
                id=rubric_id,
                criteria=[Criteria.model_validate(dict(item)) for item in criteria_rows],
            )
            if rubric_id is not None
            else None
        )
        return Question(
            **{key: value for key, value in dict(question_row).items() if key != "rubric_id"},
            rubric=rubric,
        )

    @classmethod
    def _to_test(
        cls,
        test_row: RowMapping,
        question_rows: list[RowMapping],
        criteria_by_rubric: dict[str, list[RowMapping]],
    ) -> Test:
        return Test(
            **dict(test_row),
            questions=[
                cls._to_question(row, criteria_by_rubric.get(row["rubric_id"], []))
                for row in question_rows
            ],
        )

    @staticmethod
    def _to_response(
        response_row: RowMapping, criteria_met_rows: list[RowMapping]
    ) -> Response:
        values = {key: value for key, value in dict(response_row).items() if key != "position"}
        return Response(
            **values,
            criteria_met=[CriteriaMet.model_validate(dict(row)) for row in criteria_met_rows],
        )
