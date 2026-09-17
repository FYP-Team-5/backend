from __future__ import annotations

import asyncio
import logging

from app.config import Settings
from app.db import AttemptStateError, PostgresGradingRepository
from app.dto import (
    AttemptGradeResponse,
    CourseCreate,
    GradeAttemptRequest,
    RubricCreate,
    TestCreate,
)
from app.model import Attempt, Course, Question, Response, Test
from app.service.csv_import import parse_criteria_csv, parse_questions_csv
from app.service.llm_client import LocalLLMClient

SYSTEM_PROMPT = """You are a strict grading assistant.
Evaluate whether the student answer fulfills each grading criterion independently.
Return ONLY a valid JSON object with numeric score, string feedback, and a
criteria_met array containing one object per criterion. Each criteria_met object
must contain the criterion's id as criteria_id and a boolean is_met. Do not
include explanations or markdown outside the JSON object.
"""

logger = logging.getLogger(__name__)


class StudentAnswerTooLargeError(ValueError):
    pass


class IncompleteAttemptError(ValueError):
    pass


class UnknownQuestionError(ValueError):
    pass


class RubricNotAssignedError(ValueError):
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
        self._grading_tasks: dict[str, asyncio.Task[None]] = {}

    async def initialize(self) -> None:
        await asyncio.to_thread(self.grading_store.initialize)

    async def close(self) -> None:
        if self._grading_tasks:
            await asyncio.gather(
                *list(self._grading_tasks.values()), return_exceptions=True
            )
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
    ) -> Test:
        questions = parse_questions_csv(csv_content)
        request = TestCreate(
            test_name=test_name,
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

    async def get_test(self, test_id: str) -> Test:
        return await asyncio.to_thread(self.grading_store.get_test, test_id)

    async def set_question_rubric(
        self, test_id: str, question_id: str, request: RubricCreate
    ) -> Question:
        return await asyncio.to_thread(
            self.grading_store.set_question_rubric, test_id, question_id, request
        )

    async def create_attempt(self, test_id: str, user_id: str) -> Attempt:
        test = await self.get_test(test_id)
        missing = [question.id for question in test.questions if question.rubric is None]
        if missing:
            raise RubricNotAssignedError(
                f"Cannot start an attempt; question(s) missing a rubric: {missing}."
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
        """Saves the submitted answers and starts grading them in the
        background. Returns immediately with the attempt in "grading" status
        — poll get_attempt_result for the outcome instead of waiting here,
        since each answer requires its own LLM round-trip.
        """
        attempt = await asyncio.to_thread(self.grading_store.get_attempt, attempt_id)
        self._validate_attempt(attempt, test_id, user_id)
        existing_task = self._grading_tasks.get(attempt_id)
        if existing_task is not None and not existing_task.done():
            raise AttemptStateError("This attempt is already being graded.")
        test = await self.get_test(test_id)
        questions_by_id = {question.id: question for question in test.questions}

        to_grade: list[tuple[str, Question, str]] = []
        for submission in request.responses:
            question = questions_by_id.get(submission.question_id)
            if question is None:
                raise UnknownQuestionError(
                    f"Question '{submission.question_id}' does not belong to test '{test_id}'."
                )
            answer = submission.answer.strip()
            if len(answer) > self.settings.max_answer_characters:
                raise StudentAnswerTooLargeError(
                    f"Answer for question '{question.id}' exceeds the character limit."
                )
            response_id = await asyncio.to_thread(
                self.grading_store.save_response,
                attempt_id,
                question.id,
                answer,
            )
            to_grade.append((response_id, question, answer))

        attempt = await asyncio.to_thread(
            self.grading_store.mark_attempt_grading, attempt_id
        )
        task = asyncio.create_task(
            self._grade_in_background(
                test, attempt_id, to_grade, finalize=request.finalize
            ),
            name=f"grade-attempt-{attempt_id}",
        )
        self._grading_tasks[attempt_id] = task
        task.add_done_callback(
            lambda done_task, aid=attempt_id: self._forget_grading_task(aid, done_task)
        )
        return attempt

    async def _grade_in_background(
        self,
        test: Test,
        attempt_id: str,
        to_grade: list[tuple[str, Question, str]],
        *,
        finalize: bool,
    ) -> None:
        # Grades every submitted answer even if one fails, so a single flaky
        # LLM call doesn't discard grades already earned on other questions
        # in the same submission.
        had_failure = False
        for response_id, question, answer in to_grade:
            try:
                result = await self.llm.grade(
                    system_prompt=SYSTEM_PROMPT,
                    user_prompt=self._grading_prompt(question, answer),
                )
                if result.score > question.max_score:
                    raise LLMScoreScaleError(
                        f"LLM returned score {result.score}; "
                        f"question '{question.id}' allows at most {question.max_score}."
                    )
                known_criteria_ids = {item.id for item in question.rubric.criteria}
                returned_criteria_ids = {item.criteria_id for item in result.criteria_met}
                unknown = returned_criteria_ids - known_criteria_ids
                if unknown:
                    raise LLMCriteriaMismatchError(
                        f"LLM returned unknown criteria id(s): {sorted(unknown)}."
                    )
                await asyncio.to_thread(
                    self.grading_store.save_response_grade,
                    response_id=response_id,
                    score=result.score,
                    feedback=result.feedback,
                    criteria_met_results=[
                        {"criteria_id": item.criteria_id, "is_met": item.is_met}
                        for item in result.criteria_met
                    ],
                )
            except Exception as exc:  # noqa: BLE001 - LLM/validation failures must not crash the task
                had_failure = True
                logger.error(
                    "Grading failed for question %s on attempt %s: %s",
                    question.id,
                    attempt_id,
                    exc,
                )
                await asyncio.to_thread(
                    self.grading_store.mark_attempt_failed,
                    attempt_id,
                    f"Question '{question.id}': {type(exc).__name__}: {exc}",
                )

        if had_failure:
            return
        if not finalize:
            await asyncio.to_thread(
                self.grading_store.mark_attempt_in_progress, attempt_id
            )
            return
        responses = await asyncio.to_thread(self.grading_store.list_responses, attempt_id)
        graded_ids = {response.question_id for response in responses}
        missing = [
            question.id for question in test.questions if question.id not in graded_ids
        ]
        if missing:
            await asyncio.to_thread(
                self.grading_store.mark_attempt_failed,
                attempt_id,
                f"Cannot finalize attempt; ungraded question(s): {missing}.",
            )
            return
        await asyncio.to_thread(self.grading_store.mark_attempt_graded, attempt_id)

    def _forget_grading_task(self, attempt_id: str, task: asyncio.Task[None]) -> None:
        if self._grading_tasks.get(attempt_id) is task:
            self._grading_tasks.pop(attempt_id, None)

    async def get_attempt_result(
        self,
        test_id: str,
        attempt_id: str,
        user_id: str,
    ) -> AttemptGradeResponse:
        attempt = await asyncio.to_thread(self.grading_store.get_attempt, attempt_id)
        self._validate_attempt(attempt, test_id, user_id, allow_graded=True)
        test = await self.get_test(test_id)
        responses = await asyncio.to_thread(self.grading_store.list_responses, attempt_id)
        return self._attempt_response(test, attempt, responses)

    async def list_attempts(self, test_id: str, user_id: str) -> list[Attempt]:
        await self.get_test(test_id)
        return await asyncio.to_thread(
            self.grading_store.list_attempts,
            test_id,
            user_id,
        )

    @staticmethod
    def _validate_attempt(
        attempt: Attempt,
        test_id: str,
        user_id: str,
        *,
        allow_graded: bool = False,
    ) -> None:
        if attempt.test_id != test_id or attempt.user_id != user_id:
            raise AttemptStateError("Attempt does not belong to this user and test.")
        if attempt.status == "graded" and not allow_graded:
            raise AttemptStateError("A finalized attempt cannot be changed.")

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
    def _attempt_response(
        test: Test,
        attempt: Attempt,
        responses: list[Response],
    ) -> AttemptGradeResponse:
        total_score = sum(response.score for response in responses)
        max_score = sum(question.max_score for question in test.questions)
        return AttemptGradeResponse(
            attempt=attempt,
            responses=responses,
            total_score=total_score,
            max_score=max_score,
            percentage=round(total_score / max_score * 100, 2),
            completed_questions=len(responses),
            total_questions=len(test.questions),
        )
