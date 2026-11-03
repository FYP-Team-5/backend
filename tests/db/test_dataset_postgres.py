"""Dataset queue and activation races under real PostgreSQL row locks."""

import asyncio
import io
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine

from app.config import Settings
from app.db import PostgresGradingRepository
from app.db.grading_workflow import GradingWorkflowRepository
from app.dto import GradeAttemptRequest, SubmissionGradeUpdate, TestCreate
from app.service.dataset_service import DatasetService
from app.service.training_csv import encode_training_csv, parse_training_csv
from tests.dataset_fakes import MemoryDatasetStorage

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL is not configured"
)


@pytest.fixture
def dataset():
    url = os.environ["TEST_DATABASE_URL"]
    schema = "dataset_test_" + uuid.uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    store = PostgresGradingRepository(engine=engine)
    store.initialize()
    storage = MemoryDatasetStorage()
    service = DatasetService(Settings(dataset_retry_seconds=0), store, storage=storage)
    service.repository.initialize()
    course = store.create_course("CS", "Computer Science")
    test = store.create_test(
        course.id,
        TestCreate(
            test_name="Dataset quiz",
            ends_at=datetime.now(UTC) + timedelta(days=1),
            questions=[
                {
                    "prompt": "Explain",
                    "max_score": 4,
                    "score_increment": 1,
                    "rubric": {"criteria": [{"description": "Correct", "score": 4}]},
                }
            ],
        ),
    )
    workflow = GradingWorkflowRepository(store)
    attempt = store.create_attempt(test_id=test.id, user_id="student")
    workflow.accept_submission(
        test,
        attempt.id,
        "student",
        GradeAttemptRequest(
            responses=[{"question_id": test.questions[0].id, "answer": "Answer"}]
        ),
    )
    job = workflow.claim_job(60, 3)
    response = store.list_responses(attempt.id)[0]
    workflow.save_job_grade(
        job,
        response.id,
        4,
        "Feedback",
        [{"criteria_id": test.questions[0].rubric.criteria[0].id, "is_met": True}],
    )
    workflow.finish_job(job)
    workflow.end_test(test.id, "instructor")
    try:
        yield service, workflow, test, attempt, response
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.dispose()


def race(first, second):
    barrier = threading.Barrier(2)

    def run(operation):
        barrier.wait(timeout=5)
        return operation()

    with ThreadPoolExecutor(max_workers=2) as pool:
        left, right = [pool.submit(run, operation) for operation in (first, second)]
        return left.result(timeout=10), right.result(timeout=10)


def drain(service):
    async def process():
        for _ in range(10):
            if not await service.process_pending():
                return
        raise AssertionError("Queue did not settle")

    asyncio.run(process())


def test_postgres_duplicate_export_events_reserve_once(dataset):
    service, *_ = dataset
    race(service.repository.reconcile, service.repository.reconcile)
    assert len(service.repository.list_versions()) == 1
    jobs = race(
        lambda: service.repository.claim(60, 3), lambda: service.repository.claim(60, 3)
    )
    assert sum(job is not None for job in jobs) == 1


def test_postgres_upload_racing_with_export_preserves_both_sources(dataset):
    service, *_ = dataset
    row = {
        "id": "seed",
        "question": "Question",
        "desired_answer": "Model",
        "student_answer": "Seed answer",
        "criteria[]": ["Correct"],
        "criteria_score[]": [1],
        "criteria_max_score[]": [1],
        "student_score": 1,
    }
    race(
        service.repository.reconcile,
        lambda: asyncio.run(
            service.upload(
                io.BytesIO(encode_training_csv([row])),
                "instructor",
                "seed.csv",
                "upload-key",
            )
        ),
    )
    drain(service)
    source, _ = asyncio.run(service.download())
    with source:
        rows = parse_training_csv(source)
    assert len(rows) == 2
    assert {row["student_answer"] for row in rows} == {"Seed answer", "Answer"}
    # Replay reconciliation cannot create repeated automatic rows or versions.
    count = len(service.repository.list_versions())
    drain(service)
    assert len(service.repository.list_versions()) == count


def test_postgres_grade_edit_racing_with_activation_cannot_be_lost(dataset):
    service, workflow, test, attempt, response = dataset
    service.repository.reconcile()
    job = service.repository.claim(60, 3)
    prepared = service.repository.prepare(job)
    service.storage.put_csv(
        prepared["object_key"],
        io.BytesIO(prepared["payload"]),
        prepared["checksum"],
        prepared["byte_size"],
    )
    edit = SubmissionGradeUpdate(
        revision=workflow.store.get_attempt(attempt.id).revision,
        responses=[
            {
                "response_id": response.id,
                "score": 2,
                "feedback": "Instructor correction",
                "criterion_scores": [
                    {"criteria_id": test.questions[0].rubric.criteria[0].id, "score": 2}
                ],
            }
        ],
    )
    race(
        lambda: service.repository.activate(job, prepared),
        lambda: workflow.edit_grades(test.id, attempt.id, "instructor", edit),
    )
    drain(service)
    source, _ = asyncio.run(service.download())
    with source:
        rows = parse_training_csv(source)
    assert len(rows) == 1 and rows[0]["student_score"] == 2
