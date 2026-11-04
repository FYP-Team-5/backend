"""Real locking tests. Point TEST_DATABASE_URL at a disposable PostgreSQL server.

Every test owns a randomly named schema; existing application tables are untouched.
"""

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select

from app.db import GradingConflictError, PostgresGradingRepository
from app.db.grading_repository import attempts, grade_changes, grading_jobs, tests
from app.db.grading_workflow import GradingWorkflowRepository
from app.dto import GradeAttemptRequest, SubmissionGradeUpdate, TestCreate

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL is not configured"
)


@pytest.fixture
def workflow():
    url = os.environ["TEST_DATABASE_URL"]
    schema = "grading_test_" + uuid.uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    store = PostgresGradingRepository(engine=engine)
    store.initialize()
    course = store.create_course("CS", "Computer Science")
    test = store.create_test(
        course.id,
        TestCreate(
            test_name="Concurrent quiz",
            ends_at=datetime.now(UTC) + timedelta(days=1),
            max_attempts=3,
            questions=[
                {
                    "prompt": "Q",
                    "max_score": 4,
                    "score_increment": 1,
                    "rubric": {"criteria": [{"description": "Correct", "score": 4}]},
                }
            ],
        ),
    )
    try:
        yield GradingWorkflowRepository(store), store, test
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.dispose()


def payload(test):
    return GradeAttemptRequest(
        responses=[{"question_id": test.questions[0].id, "answer": "Answer"}]
    )


def race(first, second):
    barrier = threading.Barrier(2)

    def run(operation):
        barrier.wait(timeout=5)
        return operation()

    with ThreadPoolExecutor(max_workers=2) as pool:
        left, right = [pool.submit(run, operation) for operation in (first, second)]
        return left.result(timeout=10), right.result(timeout=10)


def test_postgres_workers_claim_exclusively(workflow):
    workflow, store, test = workflow
    attempt = store.create_attempt(test_id=test.id, user_id="student")
    workflow.accept_submission(test, attempt.id, "student", payload(test))
    first, second = race(
        lambda: workflow.claim_job(60, 3), lambda: workflow.claim_job(60, 3)
    )
    assert sum(job is not None for job in (first, second)) == 1


def test_postgres_concurrent_duplicate_submission_creates_one_job(workflow):
    workflow, store, test = workflow
    attempt = store.create_attempt(test_id=test.id, user_id="student")
    first, second = race(
        lambda: workflow.accept_submission(test, attempt.id, "student", payload(test)),
        lambda: workflow.accept_submission(test, attempt.id, "student", payload(test)),
    )
    assert first.submission_version == second.submission_version == 1
    with store.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(grading_jobs)) == 1


def test_postgres_closure_and_acceptance_are_serialized(workflow):
    workflow, store, test = workflow
    attempt = store.create_attempt(test_id=test.id, user_id="student")

    def submit():
        try:
            return workflow.accept_submission(
                test, attempt.id, "student", payload(test)
            )
        except GradingConflictError:
            return None

    accepted, ended = race(submit, lambda: workflow.end_test(test.id, "instructor"))
    assert ended.is_closed
    if accepted:
        assert accepted.submitted_at <= ended.ended_at
    else:
        assert store.list_responses(attempt.id) == []
    with pytest.raises(GradingConflictError):
        store.create_attempt(test_id=test.id, user_id="another")


def test_postgres_attempt_limit_is_serialized(workflow):
    _, store, test = workflow
    with store.engine.begin() as connection:
        connection.execute(
            tests.update().where(tests.c.id == test.id).values(max_attempts=1)
        )

    def start():
        try:
            return store.create_attempt(test_id=test.id, user_id="student")
        except GradingConflictError:
            return None

    results = race(start, start)
    assert sum(item is not None for item in results) == 1
    with store.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(attempts)) == 1


def test_postgres_latest_grade_edit_and_completion_are_serialized(workflow):
    workflow, store, test = workflow

    def prepare():
        attempt = store.create_attempt(test_id=test.id, user_id="student")
        workflow.accept_submission(test, attempt.id, "student", payload(test))
        job = workflow.claim_job(60, 3)
        response = store.list_responses(attempt.id)[0]
        workflow.save_job_grade(job, response.id, 2, "Automated", [])
        return attempt, job, response

    old, job, response = prepare()
    workflow.finish_job(job)
    newer, newer_job, _ = prepare()
    request = SubmissionGradeUpdate(
        revision=store.get_attempt(old.id).revision,
        responses=[{"response_id": response.id, "score": 3, "feedback": "Edited"}],
    )

    def edit():
        try:
            workflow.edit_grades(test.id, old.id, "instructor", request)
            return True
        except GradingConflictError:
            return False

    edited, _ = race(edit, lambda: workflow.finish_job(newer_job))
    assert store.get_attempt(newer.id).status == "graded"
    with store.engine.connect() as connection:
        audit = connection.execute(select(grade_changes)).mappings().first()
    assert bool(audit) == edited
    if edited:
        assert audit["edited_at"] <= store.get_attempt(newer.id).graded_at
