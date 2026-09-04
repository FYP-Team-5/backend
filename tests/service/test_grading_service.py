import asyncio
import re

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import AttemptStateError, PostgresGradingRepository
from app.dto import (
    CourseCreate,
    CriteriaCreate,
    CriteriaGradingResult,
    CriteriaMetResult,
    GradeAttemptRequest,
    QuestionCreate,
    QuestionResponseSubmission,
    RubricCreate,
    TestCreate,
)
from app.service import (
    GradingService,
    IncompleteAttemptError,
    LLMScoreScaleError,
    RubricNotAssignedError,
    UnknownQuestionError,
)

CRITERION_ID_PATTERN = re.compile(r'<criterion id="([^"]+)"')
MAX_SCORE_PATTERN = re.compile(r'max_score="([\d.]+)"')


class FakeLLMClient:
    def __init__(self, *, wrong_scale: bool = False) -> None:
        self.calls = []
        self.wrong_scale = wrong_scale

    async def close(self) -> None:
        pass

    async def health(self) -> bool:
        return True

    async def grade(self, **kwargs) -> CriteriaGradingResult:
        self.calls.append(kwargs)
        criteria_ids = CRITERION_ID_PATTERN.findall(kwargs["user_prompt"])
        max_score = float(MAX_SCORE_PATTERN.search(kwargs["user_prompt"]).group(1))
        score = max_score * 100 if self.wrong_scale else max_score * 0.8
        return CriteriaGradingResult(
            score=score,
            feedback="Relevant answer with room for more evidence.",
            criteria_met=[
                CriteriaMetResult(criteria_id=criteria_id, is_met=index == 0)
                for index, criteria_id in enumerate(criteria_ids)
            ],
        )


def make_grading_store() -> PostgresGradingRepository:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    repository = PostgresGradingRepository(engine=engine)
    repository.initialize()
    return repository


def make_service(*, wrong_scale: bool = False):
    grading_store = make_grading_store()
    llm = FakeLLMClient(wrong_scale=wrong_scale)
    service = GradingService(Settings(), grading_store=grading_store, llm_client=llm)
    course = asyncio.run(
        service.create_course(CourseCreate(course_code="HIST-101", course_name="History"))
    )
    test = asyncio.run(
        service.create_test(
            course.id,
            TestCreate(
                test_name="History midterm",
                max_attempts=2,
                questions=[
                    QuestionCreate(
                        prompt="Explain the cause.",
                        max_score=10,
                        score_increment=1,
                        rubric=RubricCreate(
                            criteria=[
                                CriteriaCreate(description="Accuracy", score=8),
                                CriteriaCreate(description="Evidence", score=2),
                            ]
                        ),
                    ),
                    QuestionCreate(
                        prompt="Evaluate the evidence.",
                        max_score=5,
                        score_increment=1,
                        rubric=RubricCreate(
                            criteria=[
                                CriteriaCreate(description="Accuracy", score=4),
                                CriteriaCreate(description="Evidence", score=1),
                            ]
                        ),
                    ),
                ],
            ),
        )
    )
    return service, grading_store, llm, test


def test_multi_question_attempt_is_graded_and_persisted() -> None:
    service, grading_store, llm, test = make_service()
    question1, question2 = test.questions
    attempt = asyncio.run(service.create_attempt(test.id, "student-1"))

    response = asyncio.run(
        service.grade_attempt(
            test.id,
            attempt.id,
            "student-1",
            GradeAttemptRequest(
                responses=[
                    QuestionResponseSubmission(
                        question_id=question1.id,
                        answer="Economic pressure was the main cause.",
                    ),
                    QuestionResponseSubmission(
                        question_id=question2.id,
                        answer="The source supports the conclusion.",
                    ),
                ]
            ),
        )
    )

    assert response.attempt.status == "graded"
    assert response.total_score == pytest.approx(12)
    assert response.max_score == 15
    assert response.percentage == 80
    assert response.completed_questions == 2
    assert len(llm.calls) == 2
    assert len(grading_store.list_responses(attempt.id)) == 2


def test_single_question_calls_can_share_one_attempt_before_finalization() -> None:
    service, _, _, test = make_service()
    question1, question2 = test.questions
    attempt = asyncio.run(service.create_attempt(test.id, "student-1"))

    partial = asyncio.run(
        service.grade_attempt(
            test.id,
            attempt.id,
            "student-1",
            GradeAttemptRequest(
                responses=[
                    QuestionResponseSubmission(
                        question_id=question1.id,
                        answer="First response.",
                    )
                ],
                finalize=False,
            ),
        )
    )
    final = asyncio.run(
        service.grade_attempt(
            test.id,
            attempt.id,
            "student-1",
            GradeAttemptRequest(
                responses=[
                    QuestionResponseSubmission(
                        question_id=question2.id,
                        answer="Second response.",
                    )
                ],
                finalize=True,
            ),
        )
    )

    assert partial.attempt.status == "in_progress"
    assert partial.completed_questions == 1
    assert final.attempt.status == "graded"
    assert final.completed_questions == 2


def test_attempt_cannot_finalize_with_missing_questions() -> None:
    service, _, _, test = make_service()
    question1, question2 = test.questions
    attempt = asyncio.run(service.create_attempt(test.id, "student-1"))

    with pytest.raises(IncompleteAttemptError, match=re.escape(question2.id)):
        asyncio.run(
            service.grade_attempt(
                test.id,
                attempt.id,
                "student-1",
                GradeAttemptRequest(
                    responses=[
                        QuestionResponseSubmission(
                            question_id=question1.id,
                            answer="Only one response.",
                        )
                    ],
                    finalize=True,
                ),
            )
        )


def test_attempt_ownership_is_enforced() -> None:
    service, _, _, test = make_service()
    attempt = asyncio.run(service.create_attempt(test.id, "student-1"))

    with pytest.raises(AttemptStateError, match="does not belong"):
        asyncio.run(
            service.get_attempt_result(
                test.id,
                attempt.id,
                "student-2",
            )
        )


def test_attempt_cannot_start_while_a_question_has_no_rubric() -> None:
    service, _, _, _ = make_service()
    course = asyncio.run(
        service.create_course(CourseCreate(course_code="MATH-101", course_name="Math"))
    )
    test = asyncio.run(
        service.create_test(
            course.id,
            TestCreate(
                test_name="Algebra quiz",
                max_attempts=1,
                questions=[
                    QuestionCreate(prompt="Solve for x.", max_score=10, score_increment=1)
                ],
            ),
        )
    )

    with pytest.raises(RubricNotAssignedError, match="missing a rubric"):
        asyncio.run(service.create_attempt(test.id, "student-1"))

    asyncio.run(
        service.set_question_rubric(
            test.id,
            test.questions[0].id,
            RubricCreate(criteria=[CriteriaCreate(description="Correct answer", score=10)]),
        )
    )
    attempt = asyncio.run(service.create_attempt(test.id, "student-1"))
    assert attempt.status == "in_progress"


def test_grading_rejects_question_outside_the_test() -> None:
    service, _, _, test = make_service()
    attempt = asyncio.run(service.create_attempt(test.id, "student-1"))

    with pytest.raises(UnknownQuestionError, match="does not belong"):
        asyncio.run(
            service.grade_attempt(
                test.id,
                attempt.id,
                "student-1",
                GradeAttemptRequest(
                    responses=[
                        QuestionResponseSubmission(
                            question_id="not-a-real-question",
                            answer="Response.",
                        )
                    ],
                    finalize=False,
                ),
            )
        )


def test_llm_cannot_change_question_score_scale() -> None:
    service, grading_store, _, test = make_service(wrong_scale=True)
    question1 = test.questions[0]
    attempt = asyncio.run(service.create_attempt(test.id, "student-1"))

    with pytest.raises(LLMScoreScaleError, match="allows at most 10"):
        asyncio.run(
            service.grade_attempt(
                test.id,
                attempt.id,
                "student-1",
                GradeAttemptRequest(
                    responses=[
                        QuestionResponseSubmission(
                            question_id=question1.id,
                            answer="Response.",
                        )
                    ],
                    finalize=False,
                ),
            )
        )

    assert grading_store.get_attempt(attempt.id).status == "failed"
