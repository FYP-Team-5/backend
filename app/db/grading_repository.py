from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import (
    JSON,
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
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.dto import ExampleCreate, RubricCreate, TestCreate
from app.model import (
    Attempt,
    Course,
    Criteria,
    CriteriaMet,
    Example,
    Question,
    Response,
    Rubric,
    Test,
)
from app.model.assessment import utc_datetime

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
    Column("starts_at", DateTime(timezone=True), nullable=True),
    Column("ends_at", DateTime(timezone=True), nullable=True),
    Column("ended_at", DateTime(timezone=True), nullable=True),
    Column("ended_by", String(128), nullable=True),
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
    Column("external_id", String(128), nullable=True),
    Column("position", Integer, nullable=False),
    Column("prompt", Text, nullable=False),
    Column("max_score", Float, nullable=False),
    Column("score_increment", Float, nullable=False),
    Column("model_answer", Text, nullable=True),
    Column("rubric_id", ForeignKey("grading_rubrics.id"), nullable=True, unique=True),
    Column("grading_method", String(16), nullable=True),
    UniqueConstraint("test_id", "position", name="uq_grading_question_position"),
    UniqueConstraint("test_id", "external_id", name="uq_grading_question_external_id"),
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
    Column("submitted_at", DateTime(timezone=True), nullable=True),
    Column("submission_version", Integer, nullable=False, default=0),
    Column("revision", Integer, nullable=False, default=0),
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
    Column("criterion_scores", JSON, nullable=True),
    Column("original_score", Float, nullable=True),
    Column("original_feedback", Text, nullable=True),
    Column("overridden", Boolean, nullable=False, default=False),
    Column("edited_by", String(128), nullable=True),
    Column("edited_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("attempt_id", "question_id", name="uq_grading_attempt_response"),
)

examples = Table(
    "grading_examples",
    grading_metadata,
    Column("id", String(36), primary_key=True),
    Column("question_id", ForeignKey("grading_questions.id"), nullable=False, index=True),
    Column("band", String(32), nullable=False),
    Column("example_answer", Text, nullable=False),
    Column("score", Float, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("question_id", "band", name="uq_grading_example_question_band"),
)

criteria_met = Table(
    "grading_criteria_met",
    grading_metadata,
    Column("id", String(36), primary_key=True),
    Column("response_id", ForeignKey("grading_responses.id"), nullable=False, index=True),
    Column("criteria_id", ForeignKey("grading_criteria.id"), nullable=False, index=True),
    Column("is_met", Boolean, nullable=False),
)

grading_jobs = Table(
    "grading_jobs",
    grading_metadata,
    Column("id", String(36), primary_key=True),
    Column("attempt_id", ForeignKey("grading_attempts.id"), nullable=False, index=True),
    Column("submission_version", Integer, nullable=False),
    Column("status", String(16), nullable=False, index=True),
    Column("snapshot", JSON, nullable=False),
    Column("request", JSON, nullable=False),
    Column("claim_count", Integer, nullable=False, default=0),
    Column("next_retry_at", DateTime(timezone=True), nullable=False, index=True),
    Column("lease_token", String(36), nullable=True),
    Column("lease_expires_at", DateTime(timezone=True), nullable=True),
    Column("last_error", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("attempt_id", "submission_version", name="uq_grading_job_version"),
)

grade_changes = Table(
    "grading_grade_changes",
    grading_metadata,
    Column("id", String(36), primary_key=True),
    Column("response_id", ForeignKey("grading_responses.id"), nullable=False, index=True),
    Column("editor_id", String(128), nullable=False),
    Column("edited_at", DateTime(timezone=True), nullable=False),
    Column("old_score", Float, nullable=False),
    Column("new_score", Float, nullable=False),
    Column("old_feedback", Text, nullable=True),
    Column("new_feedback", Text, nullable=False),
    Column("old_criterion_scores", JSON, nullable=True),
    Column("new_criterion_scores", JSON, nullable=True),
)


class GradingStoreError(RuntimeError):
    pass


class GradingRecordNotFoundError(KeyError):
    pass


class GradingConflictError(ValueError):
    pass


class AttemptLimitExceededError(GradingConflictError):
    pass


class AttemptStateError(ValueError):
    pass

def ensure_open(row) -> None:
    now = datetime.now(UTC)
    if row["ended_at"] is not None or (
        row["ends_at"] is not None and utc_datetime(row["ends_at"]) <= now
    ):
        raise GradingConflictError("This test is closed. Results remain available.")
    starts_at = row["starts_at"]
    if starts_at is not None and now < utc_datetime(starts_at):
        raise GradingConflictError("This test has not opened yet.")


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
        with self.engine.begin() as connection:
            column_type = (
                "TIMESTAMP WITH TIME ZONE"
                if self.engine.dialect.name == "postgresql"
                else "DATETIME"
            )
            migrations = {
                "grading_tests": (
                    ("starts_at", f"{column_type} NULL"),
                    ("ends_at", f"{column_type} NULL"),
                    ("ended_at", f"{column_type} NULL"),
                    ("ended_by", "VARCHAR(128) NULL"),
                ),
                "grading_attempts": (
                    ("submitted_at", f"{column_type} NULL"),
                    ("submission_version", "INTEGER NOT NULL DEFAULT 0"),
                    ("revision", "INTEGER NOT NULL DEFAULT 0"),
                ),
                "grading_responses": (
                    ("criterion_scores", "JSON NULL"),
                    ("original_score", "FLOAT NULL"),
                    ("original_feedback", "TEXT NULL"),
                    ("overridden", "BOOLEAN NOT NULL DEFAULT FALSE"),
                    ("edited_by", "VARCHAR(128) NULL"),
                    ("edited_at", f"{column_type} NULL"),
                ),
            }
            for table_name, columns in migrations.items():
                existing_columns = {
                    column["name"]
                    for column in inspect(connection).get_columns(table_name)
                }
                for column_name, definition in columns:
                    if column_name not in existing_columns:
                        connection.execute(
                            text(
                                f"ALTER TABLE {table_name} ADD COLUMN "
                                f"{column_name} {definition}"
                            )
                        )

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
                        starts_at=request.starts_at,
                        ends_at=request.ends_at,
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
                            "external_id": question.external_id,
                            "position": position,
                            "prompt": question.prompt,
                            "max_score": question.max_score,
                            "score_increment": question.score_increment,
                            "model_answer": question.model_answer,
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

    def delete_test(self, test_id: str) -> None:
        with self.engine.begin() as connection:
            question_ids = connection.execute(
                select(questions.c.id).where(questions.c.test_id == test_id)
            ).scalars().all()
            rubric_ids = connection.execute(
                select(questions.c.rubric_id).where(
                    questions.c.test_id == test_id,
                    questions.c.rubric_id.is_not(None),
                )
            ).scalars().all()
            attempt_ids = connection.execute(
                select(attempts.c.id).where(attempts.c.test_id == test_id)
            ).scalars().all()
            response_ids = (
                connection.execute(
                    select(responses.c.id).where(
                        responses.c.attempt_id.in_(attempt_ids)
                    )
                ).scalars().all()
                if attempt_ids
                else []
            )
            if response_ids:
                connection.execute(
                    delete(grade_changes).where(
                        grade_changes.c.response_id.in_(response_ids)
                    )
                )
                connection.execute(
                    delete(criteria_met).where(
                        criteria_met.c.response_id.in_(response_ids)
                    )
                )
                connection.execute(
                    delete(responses).where(responses.c.id.in_(response_ids))
                )
            if attempt_ids:
                connection.execute(
                    delete(grading_jobs).where(
                        grading_jobs.c.attempt_id.in_(attempt_ids)
                    )
                )
                connection.execute(
                    delete(attempts).where(attempts.c.id.in_(attempt_ids))
                )
            if question_ids:
                connection.execute(
                    delete(examples).where(examples.c.question_id.in_(question_ids))
                )
                connection.execute(
                    delete(questions).where(questions.c.id.in_(question_ids))
                )
            if rubric_ids:
                connection.execute(
                    delete(criteria).where(criteria.c.rubric_id.in_(rubric_ids))
                )
                connection.execute(
                    delete(rubrics).where(rubrics.c.id.in_(rubric_ids))
                )
            connection.execute(delete(tests).where(tests.c.id == test_id))

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
                update_values = {"rubric_id": new_rubric_id}
                if request.model_answer is not None:
                    update_values["model_answer"] = request.model_answer
                connection.execute(
                    update(questions)
                    .where(questions.c.id == question_id)
                    .values(**update_values)
                )
                # Historical jobs and criterion results still reference old rubrics.
        except IntegrityError as exc:
            raise GradingConflictError("Rubric identifiers already exist.") from exc
        return self._get_question(question_id)

    def set_question_examples(
        self, test_id: str, question_id: str, request: list[ExampleCreate]
    ) -> Question:
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            question_row = (
                connection.execute(
                    select(questions.c.id)
                    .where(questions.c.id == question_id, questions.c.test_id == test_id)
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if question_row is None:
                raise GradingRecordNotFoundError(question_id)
            connection.execute(delete(examples).where(examples.c.question_id == question_id))
            connection.execute(
                insert(examples),
                [
                    {
                        "id": str(uuid.uuid4()),
                        "question_id": question_id,
                        "band": item.band,
                        "example_answer": item.example_answer,
                        "score": item.score,
                        "created_at": now,
                    }
                    for item in request
                ],
            )
        return self._get_question(question_id)

    def set_grading_method(self, test_id: str, question_id: str, method: str) -> Question:
        with self.engine.begin() as connection:
            result = connection.execute(
                update(questions)
                .where(questions.c.id == question_id, questions.c.test_id == test_id)
                .values(grading_method=method)
            )
        if result.rowcount == 0:
            raise GradingRecordNotFoundError(question_id)
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
                question_ids = [row["id"] for row in question_rows]
                example_rows = (
                    connection.execute(
                        select(examples).where(examples.c.question_id.in_(question_ids))
                    )
                    .mappings()
                    .all()
                    if question_ids
                    else []
                )
        except SQLAlchemyError as exc:
            raise GradingStoreError("Unable to read test data.") from exc
        if test_row is None:
            raise GradingRecordNotFoundError(test_id)
        criteria_by_rubric: dict[str, list[RowMapping]] = {}
        for row in criteria_rows:
            criteria_by_rubric.setdefault(row["rubric_id"], []).append(row)
        examples_by_question: dict[str, list[RowMapping]] = {}
        for row in example_rows:
            examples_by_question.setdefault(row["question_id"], []).append(row)
        return self._to_test(test_row, question_rows, criteria_by_rubric, examples_by_question)

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
                ensure_open(test_row)
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

    def create_completed_attempt(
        self,
        *,
        test_id: str,
        user_id: str,
        responses_by_external_id: dict[str, tuple[str, float, str, set[int]]],
        attempt_number: int = 1,
    ) -> Attempt:
        """Create an idempotent, already-graded attempt for local demo data."""
        now = datetime.now(UTC)
        # Spread seeded attempts into the recent past so timelines look natural.
        now = now - timedelta(days=max(0, 3 - attempt_number))
        attempt_id = str(uuid.uuid4())
        # Accepted snapshot for the mock grading job, mirroring accept_submission.
        test_snapshot = self.get_test(test_id).model_dump(mode="json")
        existing_id: str | None = None
        with self.engine.begin() as connection:
            existing = connection.execute(
                select(attempts.c.id).where(
                    attempts.c.test_id == test_id,
                    attempts.c.user_id == user_id,
                    attempts.c.attempt_number == attempt_number,
                )
            ).scalar_one_or_none()
            existing_id = existing
            question_rows = connection.execute(
                select(questions)
                .where(questions.c.test_id == test_id)
                .order_by(questions.c.position)
            ).mappings().all()
            if not question_rows:
                raise GradingRecordNotFoundError(test_id)
            if existing is None:
                connection.execute(
                    insert(attempts).values(
                        id=attempt_id,
                        test_id=test_id,
                        user_id=user_id,
                        attempt_number=attempt_number,
                        status="graded",
                        started_at=now - timedelta(minutes=10),
                        submitted_at=now - timedelta(minutes=5),
                        submission_version=1,
                        revision=1,
                        graded_at=now,
                    )
                )
            payload_responses: list[dict] = []
            for question in question_rows:
                    external_id = question["external_id"]
                    answer, score, feedback, met_indexes = responses_by_external_id[
                        external_id
                    ]
                    payload_responses.append(
                        {"question_id": question["id"], "answer": answer}
                    )
                    score = min(score, question["max_score"])
                    response_row = connection.execute(
                        select(responses).where(
                            responses.c.attempt_id == (existing or attempt_id),
                            responses.c.question_id == question["id"],
                        )
                    ).mappings().first()
                    response_id = response_row["id"] if response_row else str(uuid.uuid4())
                    criterion_ids = (
                        connection.execute(
                            select(criteria.c.id)
                            .where(criteria.c.rubric_id == question["rubric_id"])
                            .order_by(criteria.c.id)
                        ).scalars().all()
                        if question["rubric_id"] is not None
                        else []
                    )
                    criterion_scores = [
                        {
                            "criteria_id": criterion_id,
                            "score": (
                                1 if index in met_indexes else 0
                            ),
                        }
                        for index, criterion_id in enumerate(criterion_ids)
                    ]
                    response_values = {
                        "answer": answer,
                        "score": score,
                        "feedback": feedback,
                        "criterion_scores": criterion_scores,
                        "original_score": score,
                        "original_feedback": feedback,
                        "overridden": False,
                        "edited_by": None,
                        "edited_at": None,
                        "updated_at": now,
                    }
                    if response_row is None:
                        connection.execute(
                            insert(responses).values(
                                id=response_id,
                                attempt_id=existing or attempt_id,
                                question_id=question["id"],
                                created_at=now - timedelta(minutes=5),
                                **response_values,
                            )
                        )
                    else:
                        connection.execute(
                            update(responses)
                            .where(responses.c.id == response_id)
                            .values(**response_values)
                        )
                    connection.execute(
                        delete(criteria_met).where(criteria_met.c.response_id == response_id)
                    )
                    if criterion_ids:
                            connection.execute(
                                insert(criteria_met),
                                [
                                {
                                    "id": str(uuid.uuid4()),
                                    "response_id": response_id,
                                    "criteria_id": criterion_id,
                                    "is_met": index in met_indexes,
                                }
                                for index, criterion_id in enumerate(criterion_ids)
                            ],
                        )
            seeded_attempt_id = existing_id or attempt_id
            job_exists = connection.execute(
                select(grading_jobs.c.id).where(
                    grading_jobs.c.attempt_id == seeded_attempt_id,
                    grading_jobs.c.submission_version == 1,
                )
            ).scalar_one_or_none()
            if job_exists is None:
                connection.execute(
                    insert(grading_jobs).values(
                        id=str(uuid.uuid4()),
                        attempt_id=seeded_attempt_id,
                        submission_version=1,
                        status="succeeded",
                        snapshot=test_snapshot,
                        request={
                            "responses": sorted(
                                payload_responses,
                                key=lambda item: item["question_id"],
                            ),
                            "finalize": True,
                        },
                        claim_count=1,
                        next_retry_at=now,
                        lease_token=None,
                        lease_expires_at=None,
                        last_error=None,
                        created_at=now - timedelta(minutes=5),
                    )
                )
        return self.get_attempt(existing_id or attempt_id)

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

    def list_responses(self, attempt_id: str) -> list[Response]:
        statement = (
            select(responses, questions.c.position)
            .join(questions, questions.c.id == responses.c.question_id)
            .where(responses.c.attempt_id == attempt_id)
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
            example_rows = (
                connection.execute(
                    select(examples).where(examples.c.question_id == question_id)
                )
                .mappings()
                .all()
            )
        return self._to_question(question_row, criteria_rows, example_rows)

    @staticmethod
    def _to_question(
        question_row: RowMapping,
        criteria_rows: list[RowMapping],
        example_rows: list[RowMapping],
    ) -> Question:
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
            examples=[Example.model_validate(dict(item)) for item in example_rows],
        )

    @classmethod
    def _to_test(
        cls,
        test_row: RowMapping,
        question_rows: list[RowMapping],
        criteria_by_rubric: dict[str, list[RowMapping]],
        examples_by_question: dict[str, list[RowMapping]],
    ) -> Test:
        return Test(
            **dict(test_row),
            questions=[
                cls._to_question(
                    row,
                    criteria_by_rubric.get(row["rubric_id"], []),
                    examples_by_question.get(row["id"], []),
                )
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
