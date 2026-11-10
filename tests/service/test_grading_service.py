import asyncio
import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import AttemptStateError, PostgresGradingRepository
from app.dto import (
    CourseCreate,
    CriteriaCreate,
    CriteriaEvaluationResult,
    CriteriaMetResult,
    FeedbackResult,
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


class FakeLLMClient:
    def __init__(self) -> None:
        self.calls = []

    async def close(self) -> None:
        pass

    async def health(self) -> bool:
        return True

    async def grade_criteria(self, **kwargs) -> CriteriaEvaluationResult:
        self.calls.append(kwargs)
        criteria_ids = CRITERION_ID_PATTERN.findall(kwargs["user_prompt"])
        return CriteriaEvaluationResult(
            criteria_met=[
                CriteriaMetResult(criteria_id=criteria_id, is_met=index == 0)
                for index, criteria_id in enumerate(criteria_ids)
            ],
        )

    async def grade_binary(self, **kwargs):
        self.calls.append(kwargs)
        return [1] + [0] * (len(kwargs["criteria"]) - 1)

    async def generate_feedback(self, **kwargs):
        return FeedbackResult(feedback="Separate feedback model response.")


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
    """Run the persisted worker job before asserting final grades."""
    await service.grade_attempt(test_id, attempt_id, user_id, request)
    await service.process_pending_job()
    return await service.get_attempt_result(test_id, attempt_id, user_id)


def make_service():
    grading_store = make_grading_store()
    llm = FakeLLMClient()
    service = GradingService(Settings(), grading_store=grading_store, llm_client=llm)
    course = asyncio.run(
        service.create_course(
            CourseCreate(course_code="HIST-101", course_name="History")
        )
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
                ends_at=datetime.now(UTC) + timedelta(days=1),
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
                    QuestionCreate(
                        prompt="Solve for x.", max_score=10, score_increment=1
                    )
                ],
                ends_at=datetime.now(UTC) + timedelta(days=1),
            ),
        )
    )

    with pytest.raises(GradingMethodNotAssignedError, match="missing a grading method"):
        asyncio.run(service.create_attempt(test.id, "student-1"))

    asyncio.run(
        service.set_question_rubric(
            test.id,
            test.questions[0].id,
            RubricCreate(
                criteria=[CriteriaCreate(description="Correct answer", score=10)]
            ),
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
        service.create_course(
            CourseCreate(course_code="CS-101", course_name="Intro CS")
        )
    )
    csv_content = (
        "id,prompt,max_score,score_increment\n"
        "1.1,What is the role of a prototype program?,1,1\n"
        "1.2,Why prototype early?,2,1\n"
    )

    test = asyncio.run(
        service.create_test_from_csv(
            course.id,
            "Quiz 1",
            1,
            csv_content,
            ends_at=datetime.now(UTC) + timedelta(days=1),
        )
    )

    assert [question.external_id for question in test.questions] == ["1.1", "1.2"]
    assert all(question.rubric is None for question in test.questions)


def test_upload_criteria_csv_attaches_rubric_and_model_answer_by_join_key() -> None:
    service, _, _, _ = make_service()
    course = asyncio.run(
        service.create_course(
            CourseCreate(course_code="CS-101", course_name="Intro CS")
        )
    )
    test = asyncio.run(
        service.create_test_from_csv(
            course.id,
            "Quiz 1",
            1,
            "id,prompt,max_score,score_increment\n"
            "1.1,What is the role of a prototype program?,1,1\n",
            ends_at=datetime.now(UTC) + timedelta(days=1),
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
        service.create_course(
            CourseCreate(course_code="CS-101", course_name="Intro CS")
        )
    )
    test = asyncio.run(
        service.create_test_from_csv(
            course.id,
            "Quiz 1",
            1,
            "id,prompt,max_score,score_increment\n1.1,Question one,1,1\n",
            ends_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    criteria_csv = "id,criteria,criteria_max_score\n9.9,Some criterion,1\n"

    with pytest.raises(UnknownQuestionError, match="9.9"):
        asyncio.run(service.upload_criteria_csv(test.id, criteria_csv))


def test_backend_composes_score_and_feedback_from_criterion_decisions() -> None:
    service, grading_store, _, test = make_service()
    service.settings.llm_grading_contract = "grading_json_v1"
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

    assert result.attempt.status == "in_progress"
    assert result.total_score == 8
    assert result.responses[0].feedback == "Separate feedback model response."
    assert grading_store.get_attempt(attempt.id).status == "in_progress"


@pytest.mark.parametrize("labels", [[1, 0], [1, 1], [0, 0]])
def test_feedback_prompt_matches_training_and_includes_every_decision(labels):
    import ast
    import json
    from pathlib import Path

    from app.service.grading_service import SYSTEM_PROMPT_FEEDBACK

    notebook = (
        Path(__file__).resolve().parents[3] / "feedback/qlora_feedback_qwen3.5-9B.ipynb"
    )
    if not notebook.exists():
        pytest.skip("Feedback training reference repository is not checked out")
    cells = json.loads(notebook.read_text())["cells"]
    source = next(
        "".join(c["source"])
        for c in cells
        if "def build_row_prompt(" in "".join(c.get("source", []))
    )
    nodes = ast.parse(source).body
    definitions = [
        n
        for n in nodes
        if (
            isinstance(n, ast.FunctionDef)
            and n.name in {"criterion_marks", "build_row_prompt"}
        )
        or (
            isinstance(n, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "STUDENT_SYSTEM_PROMPT"
                for t in n.targets
            )
        )
    ]
    training = {}
    exec(  # noqa: S102 - compare inference with the checked-out training formatter
        compile(ast.Module(body=definitions, type_ignores=[]), str(notebook), "exec"),
        training,
    )
    _, _, _, test = make_service()
    question = test.questions[0]
    question.model_answer = "Reference explanation"
    evaluation = {
        "criteria_results": [
            {"criteria_id": item.id, "is_met": bool(label)}
            for item, label in zip(question.rubric.criteria, labels)
        ]
    }
    row = {
        "question": question.prompt,
        "desired_answer": question.model_answer,
        "student_answer": "Student explanation",
        "criteria": [item.description for item in question.rubric.criteria],
        "criteria_score": labels,
        "criteria_max_score": [1, 1],
    }
    assert SYSTEM_PROMPT_FEEDBACK == training["STUDENT_SYSTEM_PROMPT"]
    assert GradingService._feedback_prompt(
        question, "Student explanation", evaluation
    ) == training["build_row_prompt"](row)


def test_binary_grader_then_feedback_model_preserves_api_and_backend_score():
    import json

    import httpx

    from app.service.llm_client import LocalLLMClient

    service, _, _, test = make_service()
    service.settings.llm_model = "grading-model"
    service.settings.llm_feedback_model = "feedback-model"
    question = test.questions[0]
    question.model_answer = "A reference explanation."
    calls = []

    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        if body["model"] == "grading-model":
            content = "[1, 0]"
        else:
            assert body["model"] == "feedback-model"
            assert (
                "1. [MET] Accuracy\n2. [NOT MET] Evidence"
                in body["messages"][1]["content"]
            )
            content = "Your explanation identifies the cause. Support it with evidence."
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}}]}
        )

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond)
        ) as transport:
            service.llm = LocalLLMClient(
                url="http://llm.test/v1/chat/completions",
                model="grading-model",
                client=transport,
            )
            attempt = await service.create_attempt(test.id, "student")
            return await submit_and_wait(
                service,
                test.id,
                attempt.id,
                "student",
                GradeAttemptRequest(
                    responses=[
                        {
                            "question_id": question.id,
                            "answer": "Economic pressure caused it.",
                        }
                    ],
                    finalize=False,
                ),
            )

    result = asyncio.run(exercise())
    assert [call["model"] for call in calls] == ["grading-model", "feedback-model"]
    assert all("response_format" not in call for call in calls)
    assert result.total_score == 8
    assert result.responses[0].original_score == 8
    assert [item.is_met for item in result.responses[0].criteria_met] == [True, False]
    assert (
        result.responses[0].feedback
        == "Your explanation identifies the cause. Support it with evidence."
    )


@pytest.mark.parametrize("decisions", [[], ["duplicate", "duplicate"], ["unknown"]])
def test_legacy_json_criteria_must_be_complete_and_unique(decisions):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    service, _, llm, test = make_service()
    service.settings.llm_grading_contract = "grading_json_v1"
    llm.grade_criteria = AsyncMock(
        return_value=SimpleNamespace(
            criteria_met=[
                CriteriaMetResult(criteria_id=value, is_met=True) for value in decisions
            ],
        )
    )
    llm.generate_feedback = AsyncMock()
    question = test.questions[0]
    attempt = asyncio.run(service.create_attempt(test.id, "student"))
    result = asyncio.run(
        submit_and_wait(
            service,
            test.id,
            attempt.id,
            "student",
            GradeAttemptRequest(
                responses=[{"question_id": question.id, "answer": "Answer"}],
                finalize=False,
            ),
        )
    )
    assert result.attempt.status == "failed"
    assert result.responses[0].score is None
    llm.generate_feedback.assert_not_called()


def test_job_from_before_model_snapshots_still_uses_json_criterion_contract():
    service, store, _, test = make_service()
    question = test.questions[0]
    attempt = asyncio.run(service.create_attempt(test.id, "student"))
    # Older queued jobs have neither a model snapshot nor a grading contract.
    service.workflow.accept_submission(
        test,
        attempt.id,
        "student",
        GradeAttemptRequest(
            responses=[{"question_id": question.id, "answer": "Saved answer"}],
            finalize=False,
        ),
    )
    asyncio.run(service.process_pending_job())
    assert store.get_attempt(attempt.id).status == "in_progress"
    response = store.list_responses(attempt.id)[0]
    assert response.score == 8
    assert response.feedback == "Separate feedback model response."


def test_retry_saved_job_accepts_criteria_only_json_and_preserves_completed_grades():
    import json

    import httpx

    from app.service.llm_client import LocalLLMClient

    service, store, _, test = make_service()
    service.settings.llm_model = "grader"
    service.settings.llm_feedback_model = "feedback"
    first, second = test.questions
    attempt = asyncio.run(service.create_attempt(test.id, "student"))
    service.workflow.accept_submission(
        test,
        attempt.id,
        "student",
        GradeAttemptRequest(
            responses=[
                {"question_id": first.id, "answer": "Already graded answer"},
                {"question_id": second.id, "answer": "Saved ungraded answer"},
            ],
        ),
    )
    job = service.workflow.claim_job(60, 3)
    first_response = store.list_responses(attempt.id)[0]
    service.workflow.save_job_grade(job, first_response.id, 8, "Saved feedback", [])
    service.workflow.finish_job(job, error="Old parser required score and feedback")
    calls = []

    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        if body["model"] == "grader":
            assert "Saved ungraded answer" in body["messages"][1]["content"]
            content = json.dumps(
                {
                    "criteria_met": [
                        {"criteria_id": criterion.id, "is_met": False}
                        for criterion in second.rubric.criteria
                    ]
                }
            )
        else:
            assert body["model"] == "feedback"
            assert "[NOT MET] Accuracy" in body["messages"][1]["content"]
            content = "Explain the idea accurately and include evidence."
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}}]}
        )

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond)
        ) as transport:
            service.llm = LocalLLMClient(
                url="http://llm.test/v1/chat/completions",
                model="grader",
                client=transport,
            )
            await service.retry_grading(test.id, attempt.id, "student")
            await service.process_pending_job()
            return await service.get_attempt_result(test.id, attempt.id, "student")

    result = asyncio.run(exercise())
    assert result.attempt.status == "graded"
    assert result.total_score == 8
    assert result.responses[0].score == 8
    assert result.responses[0].feedback == "Saved feedback"
    assert result.responses[1].score == 0
    assert (
        result.responses[1].feedback
        == "Explain the idea accurately and include evidence."
    )
    assert [call["model"] for call in calls] == ["grader", "feedback"]
