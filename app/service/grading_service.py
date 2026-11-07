from __future__ import annotations

import asyncio
import logging
import math
from contextlib import suppress
from datetime import datetime
from typing import Literal

from app.config import Settings
from app.db import (
    AttemptStateError,
    GradingRecordNotFoundError,
    PostgresGradingRepository,
)
from app.db.grading_workflow import GradingWorkflowRepository, LeaseLostError
from app.dto import (
    AttemptGradeResponse,
    CourseCreate,
    FewShotGradingResult,
    GradeAttemptRequest,
    RubricCreate,
    StudentSubmissionSummary,
    SubmissionGradeUpdate,
    TestCreate,
)
from app.model import Attempt, Course, Question, Response, Test
from app.service.csv_import import (
    parse_criteria_csv,
    parse_examples_csv,
    parse_questions_csv,
)
from app.service.llm_client import LLMResponseError, LLMServiceError, LocalLLMClient

SYSTEM_PROMPT_RUBRIC = """You are a strict grading assistant.
Evaluate whether the student answer fulfills each grading criterion independently.
Return ONLY a valid JSON object with numeric score, string feedback, and a
criteria_met array containing one object per criterion. Each criteria_met object
must contain the criterion's id as criteria_id and a boolean is_met. Do not
include explanations or markdown outside the JSON object.
"""

SYSTEM_PROMPT_FEWSHOT = """You are a strict grading assistant.
Compare the student answer holistically against the example answers provided,
which show what excellent, average, and poor responses look like along with
their scores. Return ONLY a valid JSON object with a numeric score and string
feedback. Do not include explanations or markdown outside the JSON object.
"""

logger = logging.getLogger(__name__)


class StudentAnswerTooLargeError(ValueError):
    pass


class UnknownQuestionError(ValueError):
    pass


class GradingMethodNotAssignedError(ValueError):
    pass


class GradingMethodAmbiguousError(ValueError):
    pass


class ExamplesNotAssignedError(ValueError):
    pass


class LLMScoreScaleError(RuntimeError):
    pass


class LLMCriteriaMismatchError(RuntimeError):
    pass


class GradingService:
    def __init__(
        self,
        settings: Settings,
        *,
        grading_store: PostgresGradingRepository | None = None,
        llm_client: LocalLLMClient | None = None,
    ) -> None:
        self.settings = settings
        self.grading_store = grading_store or PostgresGradingRepository(
            settings.database_url
        )
        self.llm = llm_client or LocalLLMClient(
            url=settings.llm_url,
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
        )
        self.workflow = GradingWorkflowRepository(self.grading_store)
        self.model_registry = None
        self._worker_task: asyncio.Task | None = None
        self._wake = asyncio.Event()

    async def initialize(self) -> None:
        self._wake = asyncio.Event()
        await asyncio.to_thread(self.grading_store.initialize)
        self._worker_task = asyncio.create_task(
            self._worker_loop(), name="grading-worker"
        )

    async def close(self) -> None:
        if self._worker_task is not None:
            self._worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker_task
        await asyncio.gather(
            asyncio.to_thread(self.grading_store.close),
            self.llm.close(),
        )

    async def health(self) -> dict[str, bool]:
        grading_db, llm_healthy = await asyncio.gather(
            asyncio.to_thread(self.grading_store.health),
            self.llm.health(),
        )
        return {
            "postgres": grading_db,
            "llm": llm_healthy,
        }

    async def create_course(self, request: CourseCreate) -> Course:
        return await asyncio.to_thread(
            self.grading_store.create_course,
            request.course_code,
            request.course_name,
        )

    async def list_courses(self) -> list[Course]:
        return await asyncio.to_thread(self.grading_store.list_courses)

    async def create_test(self, course_id: str, request: TestCreate) -> Test:
        return await asyncio.to_thread(
            self.grading_store.create_test, course_id, request
        )

    async def list_tests(self, course_id: str) -> list[Test]:
        return await asyncio.to_thread(self.grading_store.list_tests, course_id)

    async def create_test_from_csv(
        self,
        course_id: str,
        test_name: str,
        max_attempts: int,
        csv_content: str,
        ends_at: datetime,
        starts_at: datetime | None = None,
    ) -> Test:
        questions = parse_questions_csv(csv_content)
        request = TestCreate(
            test_name=test_name,
            starts_at=starts_at,
            ends_at=ends_at,
            max_attempts=max_attempts,
            questions=questions,
        )
        return await self.create_test(course_id, request)

    async def upload_criteria_csv(self, test_id: str, csv_content: str) -> Test:
        rubrics_by_external_id = parse_criteria_csv(csv_content)
        test = await self.get_test(test_id)
        question_by_external_id = {
            question.external_id: question
            for question in test.questions
            if question.external_id is not None
        }
        unknown = set(rubrics_by_external_id) - set(question_by_external_id)
        if unknown:
            raise UnknownQuestionError(
                f"Criteria CSV references unknown question id(s): {sorted(unknown)}."
            )
        for external_id, rubric in rubrics_by_external_id.items():
            question = question_by_external_id[external_id]
            await self.set_question_rubric(test_id, question.id, rubric)
        return await self.get_test(test_id)

    async def upload_examples_csv(self, test_id: str, csv_content: str) -> Test:
        examples_by_external_id = parse_examples_csv(csv_content)
        test = await self.get_test(test_id)
        question_by_external_id = {
            question.external_id: question
            for question in test.questions
            if question.external_id is not None
        }
        unknown = set(examples_by_external_id) - set(question_by_external_id)
        if unknown:
            raise UnknownQuestionError(
                f"Examples CSV references unknown question id(s): {sorted(unknown)}."
            )
        for external_id, question_examples in examples_by_external_id.items():
            question = question_by_external_id[external_id]
            await asyncio.to_thread(
                self.grading_store.set_question_examples,
                test_id,
                question.id,
                question_examples,
            )
        return await self.get_test(test_id)

    async def get_test(self, test_id: str) -> Test:
        return await asyncio.to_thread(self.grading_store.get_test, test_id)

    async def set_question_rubric(
        self, test_id: str, question_id: str, request: RubricCreate
    ) -> Question:
        return await asyncio.to_thread(
            self.grading_store.set_question_rubric, test_id, question_id, request
        )

    async def set_grading_method(
        self, test_id: str, question_id: str, method: Literal["rubric", "fewshot"]
    ) -> Question:
        return await asyncio.to_thread(
            self.grading_store.set_grading_method, test_id, question_id, method
        )

    @staticmethod
    def _resolve_grading_method(question: Question) -> Literal["rubric", "fewshot"]:
        """Decides which method grades a question: an explicit
        grading_method choice wins (needed once both a rubric and few-shot
        examples are attached, since there's no other way to break the
        tie); otherwise whichever of the two is actually attached is used.
        """
        has_rubric = question.rubric is not None
        has_examples = bool(question.examples)
        if question.grading_method == "rubric":
            if not has_rubric:
                raise GradingMethodNotAssignedError(
                    f"Question '{question.id}' is set to grade with a rubric, "
                    "but none is attached."
                )
            return "rubric"
        if question.grading_method == "fewshot":
            if not has_examples:
                raise GradingMethodNotAssignedError(
                    f"Question '{question.id}' is set to grade with few-shot examples, "
                    "but none are attached."
                )
            return "fewshot"
        if has_rubric and has_examples:
            raise GradingMethodAmbiguousError(
                f"Question '{question.id}' has both a rubric and few-shot examples; "
                "choose a grading method before starting an attempt."
            )
        if has_rubric:
            return "rubric"
        if has_examples:
            return "fewshot"
        raise GradingMethodNotAssignedError(
            f"Question '{question.id}' has neither a rubric nor few-shot examples attached."
        )

    async def create_attempt(self, test_id: str, user_id: str) -> Attempt:
        test = await self.get_test(test_id)
        unresolved: list[str] = []
        ambiguous: list[str] = []
        for question in test.questions:
            try:
                self._resolve_grading_method(question)
            except GradingMethodAmbiguousError:
                ambiguous.append(question.id)
            except GradingMethodNotAssignedError:
                unresolved.append(question.id)
        if ambiguous:
            raise GradingMethodAmbiguousError(
                "Cannot start an attempt; question(s) have both a rubric and "
                f"few-shot examples and need an explicit grading method chosen: {ambiguous}."
            )
        if unresolved:
            raise GradingMethodNotAssignedError(
                f"Cannot start an attempt; question(s) missing a grading method: {unresolved}."
            )
        return await asyncio.to_thread(
            self.grading_store.create_attempt,
            test_id=test_id,
            user_id=user_id,
        )

    async def grade_attempt(
        self,
        test_id: str,
        attempt_id: str,
        user_id: str,
        request: GradeAttemptRequest,
    ) -> Attempt:
        attempt = await asyncio.to_thread(self.grading_store.get_attempt, attempt_id)
        self._validate_attempt(attempt, test_id, user_id)
        test = await self.get_test(test_id)
        questions_by_id = {question.id: question for question in test.questions}
        normalized = request.model_copy(deep=True)
        for submission in normalized.responses:
            if submission.question_id not in questions_by_id:
                raise UnknownQuestionError(
                    f"Question '{submission.question_id}' does not belong to test '{test_id}'."
                )
            submission.answer = submission.answer.strip()
            if len(submission.answer) > self.settings.max_answer_characters:
                raise StudentAnswerTooLargeError("Answer exceeds the character limit.")
        model_config = (
            await asyncio.to_thread(self.model_registry.active_config)
            if self.model_registry
            else None
        ) or {
            "name": self.settings.llm_model,
            "contract": "grading_json_v1",
            "fewshot_name": self.settings.llm_model,
        }
        accepted = await asyncio.to_thread(
            self.workflow.accept_submission,
            test,
            attempt_id,
            user_id,
            normalized,
            model_config,
        )
        self._wake.set()
        return accepted

    async def retry_grading(
        self, test_id: str, attempt_id: str, user_id: str
    ) -> Attempt:
        attempt = await asyncio.to_thread(
            self.workflow.retry_grading, test_id, attempt_id, user_id
        )
        self._wake.set()
        return attempt

    async def _worker_loop(self) -> None:
        while True:
            self._wake.clear()
            try:
                if await self.process_pending_job():
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Grading worker could not process pending work")
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=self.settings.grading_poll_seconds
                )
            except TimeoutError:
                pass

    async def process_pending_job(self) -> bool:
        """Process a persisted job, also used by deterministic worker tests."""
        job = await asyncio.to_thread(
            self.workflow.claim_job,
            self.settings.grading_lease_seconds,
            self.settings.grading_max_retries,
        )
        if job is None:
            return False
        heartbeat = asyncio.create_task(self._renew_job_lease(job))
        try:
            if job["exhausted"]:
                await asyncio.to_thread(
                    self.workflow.finish_job,
                    job,
                    error="Grading recovery retry limit reached.",
                )
            else:
                await self._process_job(job)
        except asyncio.CancelledError:
            await asyncio.to_thread(self.workflow.release_job, job)
            raise
        except LeaseLostError:
            logger.info("Grading lease lost for job %s", job["id"])
        except Exception as exc:
            logger.exception("Unexpected grading job error")
            with suppress(LeaseLostError):
                await asyncio.to_thread(
                    self.workflow.finish_job,
                    job,
                    error=f"Grading interrupted: {type(exc).__name__}",
                    retry=job["claim_count"] <= self.settings.grading_max_retries,
                    delay=self.settings.grading_retry_seconds
                    * 2 ** (job["claim_count"] - 1),
                )
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        return True

    async def _renew_job_lease(self, job: dict) -> None:
        while True:
            await asyncio.sleep(self.settings.grading_lease_seconds / 3)
            try:
                owned = await asyncio.to_thread(
                    self.workflow.renew_lease,
                    job["id"],
                    job["lease_token"],
                    self.settings.grading_lease_seconds,
                )
                if not owned:
                    return
            except Exception:
                logger.exception("Could not renew grading lease")
                return

    async def _process_job(self, job: dict) -> None:
        test = Test.model_validate(job["snapshot"])
        model_config = job["snapshot"].get("_grading_model", {})
        questions_by_id = {question.id: question for question in test.questions}
        saved = await asyncio.to_thread(
            self.grading_store.list_responses, job["attempt_id"]
        )
        requested_ids = {item["question_id"] for item in job["request"]["responses"]}
        errors: list[str] = []
        permanent_failure = False
        for response in saved:
            if (
                response.score is not None
                or response.overridden
                or response.question_id not in requested_ids
            ):
                continue
            question = questions_by_id[response.question_id]
            try:
                method = self._resolve_grading_method(question)
                if method == "rubric":
                    if self.model_registry:
                        await self.model_registry.ensure_model(model_config)
                    if model_config.get("contract") == "criteria_array_v1":
                        from app.dto import CriteriaGradingResult

                        criteria = question.rubric.criteria
                        labels = await self.llm.grade_binary(
                            question=question.prompt,
                            desired_answer=question.model_answer or "",
                            student_answer=response.answer,
                            criteria=[item.description for item in criteria],
                            model=model_config["name"],
                        )
                        result = CriteriaGradingResult(
                            score=sum(
                                item.score * label
                                for item, label in zip(criteria, labels)
                            ),
                            feedback="\n".join(
                                f"{'Met' if label else 'Not met'}: {item.description}"
                                for item, label in zip(criteria, labels)
                            ),
                            criteria_met=[
                                {"criteria_id": item.id, "is_met": bool(label)}
                                for item, label in zip(criteria, labels)
                            ],
                        )
                    else:
                        overrides = (
                            {"model": model_config["name"]}
                            if model_config.get("name")
                            and model_config["name"] != self.settings.llm_model
                            else {}
                        )
                        result = await self.llm.grade(
                            system_prompt=SYSTEM_PROMPT_RUBRIC,
                            user_prompt=self._grading_prompt(question, response.answer),
                            **overrides,
                        )
                    known_ids = {item.id for item in question.rubric.criteria}
                    unknown = {
                        item.criteria_id for item in result.criteria_met
                    } - known_ids
                    if unknown:
                        raise LLMCriteriaMismatchError(
                            f"LLM returned unknown criteria id(s): {sorted(unknown)}."
                        )
                    criteria_results = [
                        item.model_dump() for item in result.criteria_met
                    ]
                else:
                    fewshot_model = model_config.get(
                        "fewshot_name", self.settings.llm_model
                    )
                    overrides = (
                        {"model": fewshot_model}
                        if fewshot_model != self.settings.llm_model
                        else {}
                    )
                    result = await self.llm.grade_fewshot(
                        system_prompt=SYSTEM_PROMPT_FEWSHOT,
                        user_prompt=self._fewshot_prompt(question, response.answer),
                        **overrides,
                    )
                    criteria_results = []
                self._validate_score_scale(result.score, question)
                await asyncio.to_thread(
                    self.workflow.save_job_grade,
                    job,
                    response.id,
                    result.score,
                    result.feedback,
                    criteria_results,
                )
            except LeaseLostError:
                raise
            except Exception as exc:
                logger.exception("Question %s failed to grade", question.id)
                errors.append(f"Question '{question.id}': {type(exc).__name__}: {exc}")
                if not isinstance(exc, LLMServiceError) or isinstance(
                    exc, LLMResponseError
                ):
                    permanent_failure = True
        if errors:
            retry = (
                not permanent_failure
                and job["claim_count"] <= self.settings.grading_max_retries
            )
            await asyncio.to_thread(
                self.workflow.finish_job,
                job,
                error="; ".join(errors)[:2000],
                retry=retry,
                delay=self.settings.grading_retry_seconds
                * 2 ** (job["claim_count"] - 1),
            )
            return
        saved = await asyncio.to_thread(
            self.grading_store.list_responses, job["attempt_id"]
        )
        completed = {item.question_id for item in saved if item.score is not None}
        missing = set(questions_by_id) - completed
        error = (
            f"Cannot finalize attempt; ungraded question(s): {sorted(missing)}."
            if missing and job["request"]["finalize"]
            else None
        )
        await asyncio.to_thread(self.workflow.finish_job, job, error=error)

    async def set_deadline(self, test_id: str, ends_at: datetime) -> Test:
        return await asyncio.to_thread(self.workflow.set_deadline, test_id, ends_at)

    async def end_test(self, test_id: str, instructor_id: str) -> Test:
        return await asyncio.to_thread(self.workflow.end_test, test_id, instructor_id)

    async def get_submission(
        self, test_id: str, attempt_id: str
    ) -> AttemptGradeResponse:
        attempt = await asyncio.to_thread(self.grading_store.get_attempt, attempt_id)
        if attempt.test_id != test_id or attempt.submitted_at is None:
            raise GradingRecordNotFoundError(attempt_id)
        result = await self.get_attempt_result(test_id, attempt_id, attempt.user_id)
        submissions = await asyncio.to_thread(self.workflow.submitted_attempts, test_id)
        latest = next(
            (
                item
                for item in submissions
                if item.user_id == attempt.user_id and item.status == "graded"
            ),
            None,
        )
        result.can_edit = latest is not None and latest.id == attempt_id
        return result

    async def edit_submission(
        self,
        test_id: str,
        attempt_id: str,
        editor_id: str,
        request: SubmissionGradeUpdate,
    ) -> AttemptGradeResponse:
        await asyncio.to_thread(
            self.workflow.edit_grades, test_id, attempt_id, editor_id, request
        )
        return await self.get_submission(test_id, attempt_id)

    async def list_submissions(
        self, test_id: str, students
    ) -> list[StudentSubmissionSummary]:
        test = await self.get_test(test_id)
        submissions = await asyncio.to_thread(self.workflow.submitted_attempts, test_id)
        latest, latest_graded = {}, {}
        for attempt in submissions:
            latest.setdefault(attempt.user_id, attempt)
            if attempt.status == "graded":
                latest_graded.setdefault(attempt.user_id, attempt)
        summaries = []
        for student in students:
            attempt = latest.get(student.id)
            result = (
                await self.get_attempt_result(test_id, attempt.id, student.id)
                if attempt
                else None
            )
            summaries.append(
                StudentSubmissionSummary(
                    user_id=student.id,
                    full_name=student.full_name,
                    student_number=student.student_number,
                    latest_submission=attempt,
                    latest_graded_submission=latest_graded.get(student.id),
                    total_score=result.total_score
                    if attempt and attempt.status == "graded"
                    else None,
                    max_score=result.max_score
                    if result
                    else sum(q.max_score for q in test.questions),
                )
            )
        return summaries

    async def get_attempt_result(
        self,
        test_id: str,
        attempt_id: str,
        user_id: str,
    ) -> AttemptGradeResponse:
        attempt = await asyncio.to_thread(self.grading_store.get_attempt, attempt_id)
        self._validate_attempt(attempt, test_id, user_id)
        test = await asyncio.to_thread(
            self.workflow.snapshot_for_attempt, attempt_id
        ) or await self.get_test(test_id)
        responses = await asyncio.to_thread(
            self.grading_store.list_responses, attempt_id
        )
        return self._attempt_response(test, attempt, responses)

    async def list_attempts(self, test_id: str, user_id: str) -> list[Attempt]:
        await self.get_test(test_id)
        return await asyncio.to_thread(
            self.grading_store.list_attempts,
            test_id,
            user_id,
        )

    async def grade_fewshot(
        self, test_id: str, question_id: str, answer: str
    ) -> FewShotGradingResult:
        """Grades a single answer via few-shot prompting instead of the
        rubric/criteria pipeline. Does not touch attempts or persist
        anything — intended for comparing the two grading methods, not for
        the student-facing attempt flow.
        """
        test = await self.get_test(test_id)
        question = next(
            (item for item in test.questions if item.id == question_id), None
        )
        if question is None:
            raise UnknownQuestionError(
                f"Question '{question_id}' does not belong to test '{test_id}'."
            )
        if not question.examples:
            raise ExamplesNotAssignedError(
                f"Question '{question_id}' has no few-shot examples assigned."
            )
        answer = answer.strip()
        if len(answer) > self.settings.max_answer_characters:
            raise StudentAnswerTooLargeError(
                f"Answer for question '{question_id}' exceeds the character limit."
            )
        result = await self.llm.grade_fewshot(
            system_prompt=SYSTEM_PROMPT_FEWSHOT,
            user_prompt=self._fewshot_prompt(question, answer),
        )
        self._validate_score_scale(result.score, question)
        return result

    @staticmethod
    def _validate_score_scale(score: float, question: Question) -> None:
        if not math.isfinite(score) or score < 0 or score > question.max_score:
            raise LLMScoreScaleError(
                f"LLM returned score {score}; "
                f"question '{question.id}' allows at most {question.max_score}."
            )

    @staticmethod
    def _validate_attempt(
        attempt: Attempt,
        test_id: str,
        user_id: str,
    ) -> None:
        if attempt.test_id != test_id or attempt.user_id != user_id:
            raise AttemptStateError("Attempt does not belong to this user and test.")

    @staticmethod
    def _grading_prompt(question: Question, answer: str) -> str:
        criteria_block = "\n\n".join(
            f'<criterion id="{item.id}" score="{item.score}">\n{item.description}\n</criterion>'
            for item in question.rubric.criteria
        )
        return f"""<criteria>
{criteria_block}
</criteria>

<question id="{question.id}" max_score="{question.max_score}">
{question.prompt}
</question>

<student_answer>
{answer}
</student_answer>

Evaluate every criterion and use max_score={question.max_score}. Return JSON only."""

    @staticmethod
    def _fewshot_prompt(question: Question, answer: str) -> str:
        band_order = {"excellent": 0, "average": 1, "poor": 2}
        ordered_examples = sorted(
            question.examples, key=lambda item: band_order[item.band]
        )
        examples_block = "\n\n".join(
            f'<example band="{item.band}" score="{item.score}">\n{item.example_answer}\n</example>'
            for item in ordered_examples
        )
        return f"""<examples>
{examples_block}
</examples>

<question id="{question.id}" max_score="{question.max_score}">
{question.prompt}
</question>

<student_answer>
{answer}
</student_answer>

Score the student answer out of max_score={question.max_score}. Return JSON only."""

    @staticmethod
    def _attempt_response(
        test: Test,
        attempt: Attempt,
        responses: list[Response],
    ) -> AttemptGradeResponse:
        total_score = sum(
            response.score for response in responses if response.score is not None
        )
        max_score = sum(question.max_score for question in test.questions)
        return AttemptGradeResponse(
            attempt=attempt,
            responses=responses,
            total_score=total_score,
            max_score=max_score,
            percentage=round(total_score / max_score * 100, 2),
            completed_questions=sum(
                response.score is not None for response in responses
            ),
            questions=test.questions,
            total_questions=len(test.questions),
        )
