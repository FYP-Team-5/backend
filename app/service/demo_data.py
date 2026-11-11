from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.db.grading_repository import PostgresGradingRepository
from app.db.user_repository import PostgresUserRepository
from app.dto import (
    TestCreate,
)
from app.service.csv_import import parse_criteria_csv, parse_questions_csv

DEMO_COURSE_CODE = "DEMO101"
DEMO_TEST_NAMES = ("Demo Test 1", "Demo Test 2")
# Demo learners (created by UserService's startup accounts) that submit attempts.
DEMO_STUDENT_EMAILS = frozenset(
    {"student@example.com", "student2@example.com"}
)
DEMO_DATA_DIRECTORY = Path(__file__).parents[1] / "demo" / "criteria"
DEMO_TRAINING_DATASET = (
    Path(__file__).parents[1] / "demo" / "dataset" / "mock_training_data.csv"
)


class DemoDataService:
    """Seeds a small, reusable catalog for exercising the frontend locally."""

    def __init__(
        self,
        *,
        grading_store: PostgresGradingRepository,
        user_repository: PostgresUserRepository,
        dataset_service=None,
    ) -> None:
        self.grading_store = grading_store
        self.user_repository = user_repository
        self.dataset_service = dataset_service

    async def seed(self) -> None:
        courses = await asyncio.to_thread(self.grading_store.list_courses)
        course = next(
            (item for item in courses if item.course_code == DEMO_COURSE_CODE),
            None,
        )
        if course is None:
            course = await asyncio.to_thread(
                self.grading_store.create_course,
                DEMO_COURSE_CODE,
                "Demo Course",
            )

        tests = await asyncio.to_thread(self.grading_store.list_tests, course.id)
        tests_by_name = {item.test_name: item for item in tests}
        for test_name in DEMO_TEST_NAMES:
            test = tests_by_name.get(test_name)
            expected_ids = {"1.1", "1.2", "1.6"}
            if test is not None and {
                question.external_id for question in test.questions
            } != expected_ids:
                await asyncio.to_thread(self.grading_store.delete_test, test.id)
                test = None
            if test is None:
                test = await asyncio.to_thread(
                    self.grading_store.create_test,
                    course.id,
                    self._test_request(test_name),
                )
            for question in test.questions:
                await asyncio.to_thread(
                    self.grading_store.set_grading_method,
                    test.id,
                    question.id,
                    "rubric",
                )
            await asyncio.to_thread(self._seed_attempts, test.id)
        await self._seed_training_dataset()

    async def _seed_training_dataset(self) -> None:
        """Upload the ten-question demo once, including on existing demo databases."""
        if self.dataset_service is None or not DEMO_TRAINING_DATASET.is_file():
            return
        instructor = next(iter(self.user_repository.list(role="instructor")), None)
        if instructor is None:
            return
        with DEMO_TRAINING_DATASET.open("rb") as source:
            await self.dataset_service.upload(
                source,
                instructor.id,
                DEMO_TRAINING_DATASET.name,
                # A stable key migrates older demo datasets once, while keeping
                # later instructor uploads active across application restarts.
                request_key="demo-mock-training-data-v1",
            )

    @staticmethod
    def _test_request(test_name: str) -> TestCreate:
        questions_csv = (DEMO_DATA_DIRECTORY / "demo_questions.csv").read_text()
        rubrics_csv = (DEMO_DATA_DIRECTORY / "demo_rubrics.csv").read_text()
        questions = parse_questions_csv(questions_csv)
        rubrics = parse_criteria_csv(rubrics_csv)
        for question in questions:
            question.rubric = rubrics[question.external_id]
        return TestCreate(
            test_name=test_name,
            max_attempts=10,
            ends_at=datetime.now(UTC) + timedelta(days=365),
            questions=questions,
        )

    def _seed_attempts(self, test_id: str) -> None:
        students = [
            user
            for user in self.user_repository.list()
            if user.role == "student" and user.email in DEMO_STUDENT_EMAILS
        ]
        responses = {
            "1.1": (
                "A prototype simulates portions of the desired software product.",
                1,
                "Correctly explains that a prototype simulates the desired software.",
                {0},
            ),
            "1.2": (
                "Testing influences coding/implementation and solution refinement.",
                2,
                "Correctly identifies both affected life-cycle stages.",
                {0, 1},
            ),
            "1.6": (
                (
                    "Variables can be declared anywhere: local variables inside "
                    "functions and global variables outside functions."
                ),
                3,
                "Correctly covers local and global variable declarations.",
                {0, 1, 2},
            ),
        }
        partial_responses = {
            "1.1": (
                "A prototype helps show what the software might do.",
                1,
                "Correctly identifies simulation, though the explanation is brief.",
                {0},
            ),
            "1.2": (
                "Testing influences the coding or implementation stage.",
                1,
                "Identifies implementation but does not mention solution refinement.",
                {0},
            ),
            "1.6": (
                "Variables can be declared inside functions and outside functions.",
                2,
                (
                    "Identifies local and global locations but does not clearly "
                    "state that declarations can occur anywhere."
                ),
                {1, 2},
            ),
        }
        weak_responses = {
            "1.1": (
                "A prototype is an early model of the software.",
                0,
                "Does not explain that a prototype simulates the desired software.",
                set(),
            ),
            "1.2": (
                "Testing influences the implementation stage of the life cycle.",
                1,
                "Identifies implementation but not solution refinement.",
                {0},
            ),
            "1.6": (
                "Global variables are declared outside functions.",
                1,
                "Covers global declarations only; local declarations are missing.",
                {2},
            ),
        }
        profiles = {
            "student@example.com": (partial_responses, weak_responses, responses),
            "student2@example.com": (weak_responses, partial_responses, partial_responses),
        }
        for student in students:
            for attempt_number, attempt_profile in enumerate(
                profiles.get(student.email, (partial_responses,) * 3), start=1
            ):
                self.grading_store.create_completed_attempt(
                    test_id=test_id,
                    user_id=student.id,
                    responses_by_external_id=attempt_profile,
                    attempt_number=attempt_number,
                )
