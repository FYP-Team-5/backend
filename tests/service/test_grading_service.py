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
    GradingMethodNotAssignedError,
    GradingService,
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


async def submit_and_wait(service, test_id, attempt_id, user_id, request):
    """grade_attempt now returns as soon as responses are saved, before the
    background LLM grading task finishes. Tests need the final result, so
    this awaits that task (tracked in service._grading_tasks) before
    fetching it via get_attempt_result."""
    await service.grade_attempt(test_id, attempt_id, user_id, request)
    task = service._grading_tasks.get(attempt_id)
    if task is not None:
        await task
    return await service.get_attempt_result(test_id, attempt_id, user_id)


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
        submit_and_wait(
            service,
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
        submit_and_wait(
            service,
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
        submit_and_wait(
            service,
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

    result = asyncio.run(
        submit_and_wait(
            service,
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

    assert result.attempt.status == "failed"
    assert question2.id in (result.attempt.error or "")


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

    with pytest.raises(GradingMethodNotAssignedError, match="missing a grading method"):
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


def test_create_test_from_csv_creates_questions_with_external_ids() -> None:
    service, _, _, _ = make_service()
    course = asyncio.run(
        service.create_course(CourseCreate(course_code="CS-101", course_name="Intro CS"))
    )
    csv_content = (
        "id,prompt,max_score,score_increment\n"
        "1.1,What is the role of a prototype program?,1,1\n"
        "1.2,Why prototype early?,2,1\n"
    )

    test = asyncio.run(
        service.create_test_from_csv(course.id, "Quiz 1", 1, csv_content)
    )

    assert [question.external_id for question in test.questions] == ["1.1", "1.2"]
    assert all(question.rubric is None for question in test.questions)


def test_upload_criteria_csv_attaches_rubric_and_model_answer_by_join_key() -> None:
    service, _, _, _ = make_service()
    course = asyncio.run(
        service.create_course(CourseCreate(course_code="CS-101", course_name="Intro CS"))
    )
    test = asyncio.run(
        service.create_test_from_csv(
            course.id,
            "Quiz 1",
            1,
            "id,prompt,max_score,score_increment\n"
            "1.1,What is the role of a prototype program?,1,1\n",
        )
    )
    criteria_csv = (
        "id,criteria,criteria_max_score,model_answer\n"
        "1.1,Mentions simulating behaviour,1,To simulate the behaviour of the product.\n"
    )

    updated_test = asyncio.run(service.upload_criteria_csv(test.id, criteria_csv))

    question = updated_test.questions[0]
    assert question.model_answer == "To simulate the behaviour of the product."
    assert [item.description for item in question.rubric.criteria] == [
        "Mentions simulating behaviour"
    ]


def test_upload_criteria_csv_rejects_unknown_question_id() -> None:
    service, _, _, _ = make_service()
    course = asyncio.run(
        service.create_course(CourseCreate(course_code="CS-101", course_name="Intro CS"))
    )
    test = asyncio.run(
        service.create_test_from_csv(
            course.id,
            "Quiz 1",
            1,
            "id,prompt,max_score,score_increment\n1.1,Question one,1,1\n",
        )
    )
    criteria_csv = "id,criteria,criteria_max_score\n9.9,Some criterion,1\n"

    with pytest.raises(UnknownQuestionError, match="9.9"):
        asyncio.run(service.upload_criteria_csv(test.id, criteria_csv))


def test_llm_cannot_change_question_score_scale() -> None:
    service, grading_store, _, test = make_service(wrong_scale=True)
    question1 = test.questions[0]
    attempt = asyncio.run(service.create_attempt(test.id, "student-1"))

    result = asyncio.run(
        submit_and_wait(
            service,
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

    assert result.attempt.status == "failed"
    assert "allows at most 10" in (result.attempt.error or "")
    assert grading_store.get_attempt(attempt.id).status == "failed"
