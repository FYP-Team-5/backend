import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select, update

from app.config import Settings
from app.db import AttemptStateError, GradingConflictError, PostgresGradingRepository
from app.db.grading_repository import (
    grade_changes,
    grading_jobs,
    responses,
    tests,
)
from app.db.grading_workflow import GradingWorkflowRepository, LeaseLostError
from app.dto import (
    CriteriaGradingResult,
    GradeAttemptRequest,
    SubmissionGradeUpdate,
    TestCreate,
)
from app.service import GradingService
from app.service.llm_client import LLMServiceError


class LLM:
    def __init__(self):
        self.calls = []
        self.fail = set()

    async def grade(self, *, system_prompt, user_prompt):
        self.calls.append(user_prompt)
        if any(answer in user_prompt for answer in self.fail):
            raise LLMServiceError("Temporary outage")
        return CriteriaGradingResult(
            score=2, feedback="Automated feedback", criteria_met=[]
        )

    async def close(self):
        pass


@pytest.fixture
def setup(tmp_path):
    # File-backed SQLite uses separate connections for restart/recovery scenarios.
    store = PostgresGradingRepository(
        engine=create_engine(f"sqlite+pysqlite:///{tmp_path / 'grading.db'}")
    )
    store.initialize()
    course = store.create_course("CS101", "Computer Science")
    test = store.create_test(
        course.id,
        TestCreate(
            test_name="Quiz",
            ends_at=datetime.now(UTC) + timedelta(days=1),
            max_attempts=5,
            questions=[
                {
                    "prompt": f"Question {index}",
                    "max_score": 4,
                    "score_increment": 1,
                    "rubric": {"criteria": [{"description": "Correct", "score": 4}]},
                }
                for index in range(2)
            ],
        ),
    )
    llm = LLM()
    service = GradingService(
        Settings(grading_retry_seconds=0, grading_max_retries=1),
        grading_store=store,
        llm_client=llm,
    )
    yield service, store, test, llm
    store.close()


def request_for(test, **kwargs):
    return GradeAttemptRequest(
        responses=[
            {"question_id": question.id, "answer": f"Answer {index}"}
            for index, question in enumerate(test.questions)
        ],
        **kwargs,
    )


def submit(service, test, user="student"):
    attempt = asyncio.run(service.create_attempt(test.id, user))
    asyncio.run(service.grade_attempt(test.id, attempt.id, user, request_for(test)))
    return attempt


def test_answers_and_job_are_atomic_and_requests_are_idempotent(setup, monkeypatch):
    service, store, test, _ = setup
    attempt = asyncio.run(service.create_attempt(test.id, "student"))
    request = request_for(test)
    asyncio.run(service.grade_attempt(test.id, attempt.id, "student", request))
    asyncio.run(service.grade_attempt(test.id, attempt.id, "student", request))
    with store.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(grading_jobs)) == 1
        assert connection.scalar(select(func.count()).select_from(responses)) == 2
    changed = request.model_copy(deep=True)
    changed.responses[0].answer = "Changed answer"
    with pytest.raises(AttemptStateError):
        asyncio.run(service.grade_attempt(test.id, attempt.id, "student", changed))
    assert store.list_responses(attempt.id)[0].answer == "Answer 0"

    # An exception after response writes must roll back the entire acceptance.
    other = asyncio.run(service.create_attempt(test.id, "other"))

    def fail_snapshot(*args, **kwargs):
        raise RuntimeError("Snapshot failure")

    monkeypatch.setattr(type(test), "model_dump", fail_snapshot)
    with pytest.raises(RuntimeError, match="Snapshot failure"):
        service.workflow.accept_submission(test, other.id, "other", request)
    assert store.list_responses(other.id) == []
    assert store.get_attempt(other.id).status == "in_progress"


def test_restart_recovers_only_unfinished_questions_after_closure(setup):
    service, store, test, llm = setup
    attempt = submit(service, test)
    abandoned = service.workflow.claim_job(60, 3)
    saved = store.list_responses(attempt.id)
    service.workflow.save_job_grade(abandoned, saved[0].id, 3, "Already completed", [])
    with store.engine.begin() as connection:
        connection.execute(
            update(grading_jobs).values(
                lease_expires_at=datetime.now(UTC) - timedelta(seconds=1)
            )
        )
    service.workflow.end_test(test.id, "instructor")
    # A new service instance has no knowledge of the old in-memory worker.
    restarted = GradingService(Settings(), grading_store=store, llm_client=llm)
    asyncio.run(restarted.process_pending_job())
    result = asyncio.run(restarted.get_attempt_result(test.id, attempt.id, "student"))
    assert result.attempt.status == "graded"
    assert result.total_score == 5
    assert len(llm.calls) == 1
    assert result.responses[0].feedback == "Already completed"
    with pytest.raises(LeaseLostError):
        service.workflow.save_job_grade(abandoned, saved[0].id, 0, "Stale write", [])
    with pytest.raises(LeaseLostError):
        service.workflow.finish_job(abandoned, error="Stale failure")


def test_worker_leases_are_exclusive_and_renewable(setup):
    service, store, test, _ = setup
    submit(service, test)
    job = service.workflow.claim_job(60, 3)
    second = GradingWorkflowRepository(store)
    assert second.claim_job(60, 3) is None
    assert service.workflow.renew_lease(job["id"], job["lease_token"], 60)
    assert not second.renew_lease(job["id"], "wrong-token", 60)
    with store.engine.begin() as connection:
        connection.execute(
            update(grading_jobs).values(
                lease_expires_at=datetime.now(UTC) - timedelta(seconds=1)
            )
        )
    reclaimed = second.claim_job(60, 3)
    assert reclaimed["id"] == job["id"]
    assert reclaimed["lease_token"] != job["lease_token"]
    assert not service.workflow.renew_lease(job["id"], job["lease_token"], 60)


def test_bounded_retries_preserve_progress_and_use_saved_answers_after_close(setup):
    service, store, test, llm = setup
    llm.fail.add("Answer 1")
    attempt = submit(service, test)
    asyncio.run(service.process_pending_job())
    assert store.get_attempt(attempt.id).status == "grading"
    asyncio.run(service.process_pending_job())
    assert store.get_attempt(attempt.id).status == "failed"
    assert len(llm.calls) == 3  # completed question zero was skipped on retry
    service.workflow.end_test(test.id, "instructor")
    llm.fail.clear()
    asyncio.run(service.retry_grading(test.id, attempt.id, "student"))
    asyncio.run(service.retry_grading(test.id, attempt.id, "student"))
    asyncio.run(service.process_pending_job())
    assert store.get_attempt(attempt.id).status == "graded"
    assert len(llm.calls) == 4
    assert [item.answer for item in store.list_responses(attempt.id)] == [
        "Answer 0",
        "Answer 1",
    ]


def test_repeated_crashes_exhaust_recovery_limit(setup):
    service, store, test, _ = setup
    attempt = submit(service, test)
    for _ in range(2):
        service.workflow.claim_job(60, 1)
        with store.engine.begin() as connection:
            connection.execute(
                update(grading_jobs).values(
                    lease_expires_at=datetime.now(UTC) - timedelta(seconds=1)
                )
            )
    asyncio.run(service.process_pending_job())
    assert store.get_attempt(attempt.id).status == "failed"
    assert "retry limit" in store.get_attempt(attempt.id).error


def test_worker_shutdown_releases_owned_job(setup):
    service, store, test, llm = setup
    attempt = submit(service, test)

    async def scenario():
        started = asyncio.Event()

        async def slow_grade(**kwargs):
            started.set()
            await asyncio.Event().wait()

        llm.grade = slow_grade
        task = asyncio.create_task(service.process_pending_job())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    with store.engine.connect() as connection:
        job = connection.execute(select(grading_jobs)).mappings().one()
    assert job["status"] == "pending"
    assert store.get_attempt(attempt.id).status == "grading"
    assert service.workflow.claim_job(60, 3) is not None


def test_deadlines_and_manual_close_block_new_work_but_keep_results(setup):
    service, store, test, _ = setup
    attempt = submit(service, test)
    unsubmitted = asyncio.run(service.create_attempt(test.id, "other"))
    with store.engine.begin() as connection:
        connection.execute(
            update(tests).where(tests.c.id == test.id).values(ends_at=datetime.now(UTC))
        )
    assert store.get_test(test.id).is_closed
    with pytest.raises(GradingConflictError, match="closed"):
        asyncio.run(service.create_attempt(test.id, "another"))
    with pytest.raises(GradingConflictError, match="closed"):
        asyncio.run(
            service.grade_attempt(test.id, unsubmitted.id, "other", request_for(test))
        )
    asyncio.run(service.process_pending_job())
    assert (
        asyncio.run(
            service.get_attempt_result(test.id, attempt.id, "student")
        ).attempt.status
        == "graded"
    )
    ended = service.workflow.end_test(test.id, "instructor")
    again = service.workflow.end_test(test.id, "someone-else")
    assert ended.ended_at == again.ended_at
    assert again.ended_by == "instructor"
    with pytest.raises(GradingConflictError):
        service.workflow.set_deadline(test.id, datetime.now(UTC) + timedelta(days=2))


def test_grade_overrides_are_atomic_versioned_and_preserve_originals(setup):
    service, store, test, _ = setup
    attempt = submit(service, test)
    asyncio.run(service.process_pending_job())
    result = asyncio.run(service.get_submission(test.id, attempt.id))
    response = result.responses[0]
    edit = SubmissionGradeUpdate(
        revision=result.attempt.revision,
        responses=[
            {
                "response_id": response.id,
                "score": 4,
                "feedback": "Instructor feedback",
            }
        ],
    )
    revised = asyncio.run(
        service.edit_submission(test.id, attempt.id, "instructor", edit)
    )
    assert revised.total_score == 6
    assert revised.percentage == 75
    assert revised.responses[0].original_score == 2
    assert revised.responses[0].original_feedback == "Automated feedback"
    assert revised.responses[0].edited_by == "instructor"
    with pytest.raises(GradingConflictError, match="Reload"):
        asyncio.run(service.edit_submission(test.id, attempt.id, "instructor", edit))
    invalid = edit.model_copy(deep=True)
    invalid.revision = revised.attempt.revision
    invalid.responses[0].score = 1.5
    with pytest.raises(GradingConflictError, match="increments"):
        asyncio.run(service.edit_submission(test.id, attempt.id, "instructor", invalid))
    with store.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(grade_changes)) == 1
    assert store.list_responses(attempt.id)[0].score == 4
    # A newer unsubmitted or pending attempt does not hide the latest graded one.
    newer = asyncio.run(service.create_attempt(test.id, "student"))
    assert asyncio.run(service.get_submission(test.id, attempt.id)).can_edit
    asyncio.run(service.grade_attempt(test.id, newer.id, "student", request_for(test)))
    assert asyncio.run(service.get_submission(test.id, attempt.id)).can_edit
    asyncio.run(service.process_pending_job())
    assert not asyncio.run(service.get_submission(test.id, attempt.id)).can_edit
    with pytest.raises(GradingConflictError, match="latest graded"):
        asyncio.run(service.edit_submission(test.id, attempt.id, "instructor", invalid))


def test_grading_uses_accepted_configuration_snapshot(setup):
    service, store, test, llm = setup
    attempt = submit(service, test)
    from app.dto import CriteriaCreate, RubricCreate

    store.set_question_rubric(
        test.id,
        test.questions[0].id,
        RubricCreate(
            criteria=[CriteriaCreate(description="New criterion", score=4)],
        ),
    )
    asyncio.run(service.process_pending_job())
    assert "New criterion" not in llm.calls[0]
    assert (
        asyncio.run(service.get_attempt_result(test.id, attempt.id, "student"))
        .questions[0]
        .rubric
        == test.questions[0].rubric
    )


def test_polling_worker_recovers_pending_jobs_on_startup(setup):
    service, store, test, llm = setup
    attempt = submit(service, test)

    async def restart():
        restarted = GradingService(
            Settings(grading_poll_seconds=0.01), grading_store=store, llm_client=llm
        )
        await restarted.initialize()
        try:
            async with asyncio.timeout(3):
                while (
                    await restarted.get_attempt_result(test.id, attempt.id, "student")
                ).attempt.status != "graded":
                    await asyncio.sleep(0.01)
        finally:
            await restarted.close()

    asyncio.run(restart())
    assert store.get_attempt(attempt.id).status == "graded"


def test_heartbeat_prevents_reclaim_during_long_llm_request(setup):
    service, store, test, llm = setup
    service.settings.grading_lease_seconds = 3
    attempt = submit(service, test)
    original_grade = llm.grade

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_grade(**kwargs):
            started.set()
            await release.wait()
            return await original_grade(**kwargs)

        llm.grade = slow_grade
        task = asyncio.create_task(service.process_pending_job())
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            await asyncio.sleep(3.2)
            assert GradingWorkflowRepository(store).claim_job(60, 3) is None
        finally:
            release.set()
            await task

    asyncio.run(scenario())
    assert store.get_attempt(attempt.id).status == "graded"


def test_multi_response_edit_rolls_back_all_changes_when_one_score_is_invalid(setup):
    service, store, test, _ = setup
    attempt = submit(service, test)
    asyncio.run(service.process_pending_job())
    result = asyncio.run(service.get_submission(test.id, attempt.id))
    edit = SubmissionGradeUpdate(
        revision=result.attempt.revision,
        responses=[
            {"response_id": result.responses[0].id, "score": 4, "feedback": "Valid"},
            {"response_id": result.responses[1].id, "score": 5, "feedback": "Too high"},
        ],
    )
    with pytest.raises(GradingConflictError):
        asyncio.run(service.edit_submission(test.id, attempt.id, "instructor", edit))
    assert [item.score for item in store.list_responses(attempt.id)] == [2, 2]
    with store.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(grade_changes)) == 0
    assert store.get_attempt(attempt.id).revision == result.attempt.revision


def test_fewshot_worker_uses_saved_examples_and_persists_grades(setup):
    service, store, test, llm = setup
    from app.dto import ExampleCreate, FewShotGradingResult

    for question in test.questions:
        store.set_question_examples(
            test.id,
            question.id,
            [
                ExampleCreate(
                    band="excellent",
                    example_answer="Accepted exemplar",
                    score=4,
                )
            ],
        )
        store.set_grading_method(test.id, question.id, "fewshot")
    test = store.get_test(test.id)
    attempt = submit(service, test)

    async def grade_fewshot(**kwargs):
        assert "Accepted exemplar" in kwargs["user_prompt"]
        return FewShotGradingResult(score=3, feedback="Few-shot feedback")

    llm.grade_fewshot = grade_fewshot
    asyncio.run(service.process_pending_job())
    result = asyncio.run(service.get_attempt_result(test.id, attempt.id, "student"))
    assert result.attempt.status == "graded"
    assert result.total_score == 6
    assert all(item.criteria_met == [] for item in result.responses)
