"""Transactional submission, leased grading queue, closure, and moderation storage."""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, insert, or_, select, update

from app.db.grading_repository import (
    AttemptStateError,
    GradingConflictError,
    GradingRecordNotFoundError,
    attempts,
    criteria_met,
    ensure_open,
    grade_changes,
    grading_jobs,
    responses,
    tests,
)
from app.dto import GradeAttemptRequest, SubmissionGradeUpdate
from app.model import Attempt, Test
from app.model.assessment import utc_datetime


class LeaseLostError(RuntimeError):
    pass


class GradingWorkflowRepository:
    def __init__(self, store) -> None:
        self.store = store
        self.engine = store.engine

    @staticmethod
    def _test(connection, test_id):
        row = (
            connection.execute(
                select(tests).where(tests.c.id == test_id).with_for_update()
            )
            .mappings()
            .first()
        )
        if row is None:
            raise GradingRecordNotFoundError(test_id)
        return row

    @staticmethod
    def _attempt(connection, test_id, attempt_id, user_id=None):
        row = (
            connection.execute(
                select(attempts)
                .where(attempts.c.id == attempt_id, attempts.c.test_id == test_id)
                .with_for_update()
            )
            .mappings()
            .first()
        )
        if row is None or (user_id is not None and row["user_id"] != user_id):
            raise GradingRecordNotFoundError(attempt_id)
        return row

    def set_deadline(self, test_id: str, ends_at: datetime) -> Test:
        with self.engine.begin() as connection:
            row = self._test(connection, test_id)
            # Unlike attempting a test, the deadline stays editable while the
            # test is upcoming; only a finished test (ended early or expired)
            # is frozen.
            now = datetime.now(UTC)
            if row["ended_at"] is not None or (
                row["ends_at"] is not None and utc_datetime(row["ends_at"]) <= now
            ):
                raise GradingConflictError(
                    "This test is closed. Results remain available."
                )
            if ends_at <= datetime.now(UTC):
                raise GradingConflictError("End date must be in the future.")
            if row["starts_at"] is not None and ends_at <= utc_datetime(row["starts_at"]):
                raise GradingConflictError("End date must be after the start date.")
            connection.execute(
                update(tests).where(tests.c.id == test_id).values(ends_at=ends_at)
            )
        return self.store.get_test(test_id)

    def end_test(self, test_id: str, instructor_id: str) -> Test:
        with self.engine.begin() as connection:
            row = self._test(connection, test_id)
            if row["ended_at"] is None:
                connection.execute(
                    update(tests)
                    .where(tests.c.id == test_id)
                    .values(
                        ended_at=datetime.now(UTC),
                        ended_by=instructor_id,
                    )
                )
        return self.store.get_test(test_id)

    def accept_submission(
        self, test: Test, attempt_id: str, user_id: str, request: GradeAttemptRequest
    ) -> Attempt:
        payload = request.model_dump(mode="json")
        payload["responses"] = sorted(
            payload["responses"], key=lambda item: item["question_id"]
        )
        with self.engine.begin() as connection:
            test_row = self._test(connection, test.id)
            attempt = self._attempt(connection, test.id, attempt_id, user_id)
            previous = (
                connection.execute(
                    select(grading_jobs).where(
                        grading_jobs.c.attempt_id == attempt_id,
                        grading_jobs.c.submission_version
                        == attempt["submission_version"],
                    )
                )
                .mappings()
                .first()
            )
            # Repeating an accepted request is safe, including after closure.
            if previous is not None and previous["request"] == payload:
                return Attempt.model_validate(dict(attempt))
            ensure_open(test_row)
            if attempt["status"] != "in_progress":
                raise AttemptStateError(
                    "Submitted answers cannot be changed. Retry grading using saved answers."
                )
            now = datetime.now(UTC)
            for item in payload["responses"]:
                existing = (
                    connection.execute(
                        select(responses).where(
                            responses.c.attempt_id == attempt_id,
                            responses.c.question_id == item["question_id"],
                        )
                    )
                    .mappings()
                    .first()
                )
                if existing is not None and existing["score"] is not None:
                    if existing["answer"] != item["answer"]:
                        raise AttemptStateError("A graded answer cannot be changed.")
                    continue
                if existing is None:
                    connection.execute(
                        insert(responses).values(
                            id=str(uuid.uuid4()),
                            attempt_id=attempt_id,
                            question_id=item["question_id"],
                            answer=item["answer"],
                            created_at=now,
                            updated_at=now,
                        )
                    )
                else:
                    connection.execute(
                        update(responses)
                        .where(responses.c.id == existing["id"])
                        .values(
                            answer=item["answer"],
                            updated_at=now,
                        )
                    )
            version = attempt["submission_version"] + 1
            # Carry the first accepted grading configuration across partial submissions.
            snapshot = (
                previous["snapshot"]
                if previous is not None
                else test.model_dump(mode="json")
            )
            connection.execute(
                insert(grading_jobs).values(
                    id=str(uuid.uuid4()),
                    attempt_id=attempt_id,
                    submission_version=version,
                    status="pending",
                    snapshot=snapshot,
                    request=payload,
                    next_retry_at=now,
                    created_at=now,
                )
            )
            connection.execute(
                update(attempts)
                .where(attempts.c.id == attempt_id)
                .values(
                    status="grading",
                    submitted_at=now,
                    submission_version=version,
                    revision=attempt["revision"] + 1,
                    error=None,
                )
            )
        return self.store.get_attempt(attempt_id)

    def retry_grading(self, test_id: str, attempt_id: str, user_id: str) -> Attempt:
        with self.engine.begin() as connection:
            self._test(connection, test_id)
            attempt = self._attempt(connection, test_id, attempt_id, user_id)
            if attempt["status"] == "grading":
                return Attempt.model_validate(dict(attempt))
            if attempt["status"] != "failed":
                raise AttemptStateError("Only failed grading can be retried.")
            result = connection.execute(
                update(grading_jobs)
                .where(
                    grading_jobs.c.attempt_id == attempt_id,
                    grading_jobs.c.submission_version == attempt["submission_version"],
                    grading_jobs.c.status == "failed",
                )
                .values(
                    status="pending",
                    claim_count=0,
                    next_retry_at=datetime.now(UTC),
                    lease_token=None,
                    lease_expires_at=None,
                    last_error=None,
                )
            )
            if result.rowcount != 1:
                raise AttemptStateError("No failed grading job was found.")
            connection.execute(
                update(attempts)
                .where(attempts.c.id == attempt_id)
                .values(
                    status="grading",
                    error=None,
                    revision=attempt["revision"] + 1,
                )
            )
        return self.store.get_attempt(attempt_id)

    def claim_job(self, lease_seconds: int, max_retries: int) -> dict | None:
        now = datetime.now(UTC)
        eligible = or_(
            and_(
                grading_jobs.c.status == "pending", grading_jobs.c.next_retry_at <= now
            ),
            and_(
                grading_jobs.c.status == "running",
                grading_jobs.c.lease_expires_at <= now,
            ),
        )
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    select(grading_jobs)
                    .where(eligible)
                    .order_by(grading_jobs.c.next_retry_at, grading_jobs.c.created_at)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            token = str(uuid.uuid4())
            # The conditional update also protects claims on SQLite during tests.
            changed = connection.execute(
                update(grading_jobs)
                .where(
                    grading_jobs.c.id == row["id"],
                    eligible,
                )
                .values(
                    status="running",
                    lease_token=token,
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    claim_count=row["claim_count"] + 1,
                )
            )
            if changed.rowcount != 1:
                return None
            return {
                **dict(row),
                "lease_token": token,
                "claim_count": row["claim_count"] + 1,
                "exhausted": row["claim_count"] > max_retries,
            }

    def renew_lease(self, job_id: str, token: str, lease_seconds: int) -> bool:
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            result = connection.execute(
                update(grading_jobs)
                .where(
                    grading_jobs.c.id == job_id,
                    grading_jobs.c.status == "running",
                    grading_jobs.c.lease_token == token,
                    grading_jobs.c.lease_expires_at > now,
                )
                .values(lease_expires_at=now + timedelta(seconds=lease_seconds))
            )
        return result.rowcount == 1

    def _owned_job(self, connection, job):
        # All operations which also touch attempts use test -> attempt -> job lock order.
        attempt_id = job["attempt_id"]
        test_id = job["snapshot"]["id"]
        self._test(connection, test_id)
        attempt = self._attempt(connection, test_id, attempt_id)
        row = (
            connection.execute(
                select(grading_jobs)
                .where(grading_jobs.c.id == job["id"])
                .with_for_update()
            )
            .mappings()
            .one()
        )
        if (
            row["status"] != "running"
            or row["lease_token"] != job["lease_token"]
            or utc_datetime(row["lease_expires_at"]) <= datetime.now(UTC)
            or attempt["submission_version"] != job["submission_version"]
            or attempt["status"] != "grading"
        ):
            raise LeaseLostError("Grading job ownership changed.")
        return attempt

    def save_job_grade(
        self,
        job: dict,
        response_id: str,
        score: float,
        feedback: str,
        criteria_results: list[dict],
    ) -> None:
        with self.engine.begin() as connection:
            self._owned_job(connection, job)
            result = connection.execute(
                update(responses)
                .where(
                    responses.c.id == response_id,
                    responses.c.attempt_id == job["attempt_id"],
                    responses.c.score.is_(None),
                    responses.c.overridden.is_(False),
                )
                .values(
                    score=score,
                    feedback=feedback,
                    original_score=score,
                    original_feedback=feedback,
                    updated_at=datetime.now(UTC),
                )
            )
            if result.rowcount == 0:
                return
            if criteria_results:
                connection.execute(
                    insert(criteria_met),
                    [
                        {"id": str(uuid.uuid4()), "response_id": response_id, **item}
                        for item in criteria_results
                    ],
                )

    def finish_job(
        self,
        job: dict,
        *,
        error: str | None = None,
        retry: bool = False,
        delay: float = 0,
    ) -> None:
        with self.engine.begin() as connection:
            attempt = self._owned_job(connection, job)
            now = datetime.now(UTC)
            status = "pending" if retry else "failed" if error else "succeeded"
            connection.execute(
                update(grading_jobs)
                .where(grading_jobs.c.id == job["id"])
                .values(
                    status=status,
                    lease_token=None,
                    lease_expires_at=None,
                    next_retry_at=now + timedelta(seconds=delay),
                    last_error=error,
                )
            )
            attempt_status = (
                "grading"
                if retry
                else "failed"
                if error
                else ("graded" if job["request"]["finalize"] else "in_progress")
            )
            connection.execute(
                update(attempts)
                .where(attempts.c.id == job["attempt_id"])
                .values(
                    status=attempt_status,
                    error=error if attempt_status == "failed" else None,
                    graded_at=now if attempt_status == "graded" else None,
                    revision=attempt["revision"] + 1,
                )
            )

    def release_job(self, job: dict) -> None:
        # Cancellation leaves saved progress intact and makes this job immediately claimable.
        with self.engine.begin() as connection:
            connection.execute(
                update(grading_jobs)
                .where(
                    grading_jobs.c.id == job["id"],
                    grading_jobs.c.lease_token == job["lease_token"],
                    grading_jobs.c.status == "running",
                )
                .values(
                    status="pending",
                    lease_token=None,
                    lease_expires_at=None,
                    next_retry_at=datetime.now(UTC),
                )
            )

    def snapshot_for_attempt(self, attempt_id: str) -> Test | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(grading_jobs.c.snapshot)
                .where(grading_jobs.c.attempt_id == attempt_id)
                .order_by(grading_jobs.c.submission_version.desc())
                .limit(1)
            ).scalar_one_or_none()
        return Test.model_validate(row) if row else None

    def submitted_attempts(self, test_id: str) -> list[Attempt]:
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    select(attempts)
                    .where(
                        attempts.c.test_id == test_id,
                        attempts.c.submitted_at.isnot(None),
                    )
                    .order_by(
                        attempts.c.submitted_at.desc(), attempts.c.attempt_number.desc()
                    )
                )
                .mappings()
                .all()
            )
        return [Attempt.model_validate(dict(row)) for row in rows]

    def edit_grades(
        self,
        test_id: str,
        attempt_id: str,
        editor_id: str,
        request: SubmissionGradeUpdate,
    ) -> None:
        snapshot = self.snapshot_for_attempt(attempt_id) or self.store.get_test(test_id)
        question_by_id = {item.id: item for item in snapshot.questions}
        with self.engine.begin() as connection:
            self._test(connection, test_id)
            attempt = self._attempt(connection, test_id, attempt_id)
            latest_id = connection.execute(
                select(attempts.c.id)
                .where(
                    attempts.c.test_id == test_id,
                    attempts.c.user_id == attempt["user_id"],
                    attempts.c.status == "graded",
                    attempts.c.submitted_at.isnot(None),
                )
                .order_by(
                    attempts.c.submitted_at.desc(), attempts.c.attempt_number.desc()
                )
                .limit(1)
            ).scalar()
            if attempt["status"] != "graded" or latest_id != attempt_id:
                raise GradingConflictError(
                    "Only the student's latest graded submission can be edited."
                )
            if request.revision != attempt["revision"]:
                raise GradingConflictError(
                    "Grades changed since you opened this submission. Reload before editing."
                )
            now = datetime.now(UTC)
            for item in request.responses:
                row = (
                    connection.execute(
                        select(responses).where(
                            responses.c.id == item.response_id,
                            responses.c.attempt_id == attempt_id,
                        )
                    )
                    .mappings()
                    .first()
                )
                if row is None or row["score"] is None:
                    raise GradingConflictError(
                        "Response does not belong to this graded submission."
                    )
                question = question_by_id[row["question_id"]]
                criterion_scores = row["criterion_scores"]
                if item.criterion_scores is not None:
                    criteria = (
                        {
                            criterion.id: criterion
                            for criterion in question.rubric.criteria
                        }
                        if question.rubric
                        else {}
                    )
                    supplied = {
                        criterion.criteria_id: criterion.score
                        for criterion in item.criterion_scores
                    }
                    if len(supplied) != len(item.criterion_scores) or set(
                        supplied
                    ) != set(criteria):
                        raise GradingConflictError(
                            "Provide exactly one score for each accepted rubric criterion."
                        )
                    if any(
                        score > criteria[key].score for key, score in supplied.items()
                    ):
                        raise GradingConflictError(
                            "Criterion score exceeds its maximum."
                        )
                    if not math.isclose(
                        sum(supplied.values()), item.score, rel_tol=1e-9, abs_tol=1e-7
                    ):
                        raise GradingConflictError(
                            "Total score must equal the sum of criterion scores."
                        )
                    criterion_scores = [
                        criterion.model_dump() for criterion in item.criterion_scores
                    ]
                steps = item.score / question.score_increment
                if item.score > question.max_score or not math.isclose(
                    steps, round(steps), abs_tol=1e-7
                ):
                    raise GradingConflictError(
                        f"Score must be between 0 and {question.max_score} in increments of {question.score_increment}."
                    )
                connection.execute(
                    insert(grade_changes).values(
                        id=str(uuid.uuid4()),
                        response_id=item.response_id,
                        editor_id=editor_id,
                        edited_at=now,
                        old_score=row["score"],
                        new_score=item.score,
                        old_feedback=row["feedback"],
                        new_feedback=item.feedback,
                        old_criterion_scores=row["criterion_scores"],
                        new_criterion_scores=criterion_scores,
                    )
                )
                connection.execute(
                    update(responses)
                    .where(responses.c.id == item.response_id)
                    .values(
                        score=item.score,
                        feedback=item.feedback,
                        criterion_scores=criterion_scores,
                        overridden=True,
                        original_score=row["original_score"]
                        if row["original_score"] is not None
                        else row["score"],
                        original_feedback=row["original_feedback"]
                        if row["original_score"] is not None
                        else row["feedback"],
                        edited_by=editor_id,
                        edited_at=now,
                        updated_at=now,
                    )
                )
            connection.execute(
                update(attempts)
                .where(attempts.c.id == attempt_id)
                .values(
                    revision=attempt["revision"] + 1,
                )
            )
