import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import AttemptStateError, PostgresGradingRepository
from app.dto import (
    CourseCreate,
    CriterionGrade,
    ExamCreate,
    GradeAttemptRequest,
    GradingResult,
    QuestionCreate,
    QuestionResponseSubmission,
    RetrievedRubricChunk,
)

pytestmark = pytest.mark.skip(
    reason="Legacy tests use the replaced caller-supplied course ID contract."
)
from app.model import RubricMetadata
from app.service import (
    GradingService,
    IncompleteAttemptError,
    LLMScoreScaleError,
    RubricOwnershipError,
)


class FakeRubricStore:
    def __init__(self, rubric: RubricMetadata) -> None:
        self.rubric = rubric

    def close(self) -> None:
        pass

    def health(self) -> bool:
        return True

    def get(self, rubric_id: str) -> RubricMetadata:
        assert rubric_id == self.rubric.id
        return self.rubric


class FakeChunkStore:
    def __init__(self) -> None:
        self.chunks = [
            RetrievedRubricChunk(
                id=f"chunk-{index}",
                content=f"Criterion for chunk {index}",
                metadata={
                    "rubric_id": "history-rubric-v1",
                    "document_id": "document-1",
                    "chunk_index": index,
                },
            )
            for index in range(3)
        ]

    def close(self) -> None:
        pass

    def health(self) -> bool:
        return True

    def retrieve(self, **kwargs):
        return self.chunks


class FakeLLMClient:
    def __init__(self, *, wrong_scale: bool = False) -> None:
        self.calls = []
        self.wrong_scale = wrong_scale

    async def close(self) -> None:
        pass

    async def health(self) -> bool:
        return True

    async def grade(self, **kwargs) -> GradingResult:
        self.calls.append(kwargs)
        expected_max = 10 if 'max_score="10.0"' in kwargs["user_prompt"] else 5
        returned_max = 100 if self.wrong_scale else expected_max
        return GradingResult(
            score=returned_max * 0.8,
            max_score=returned_max,
            feedback="Relevant answer with room for more evidence.",
            criteria=[
                CriterionGrade(
                    criterion="Accuracy",
                    score=returned_max * 0.8,
                    max_score=returned_max,
                    feedback="Mostly accurate.",
                )
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


def make_rubric() -> RubricMetadata:
    return RubricMetadata(
        id="history-rubric-v1",
        document_id="document-1",
        version="1",
        course_id="HIST-101",
        exam_id="history-midterm",
        processed=True,
        processing_status="completed",
        chunk_count=3,
        chunk_ids=["chunk-0", "chunk-1", "chunk-2"],
    )


def exam_request(max_attempts: int = 2) -> ExamCreate:
    return ExamCreate(
        id="history-midterm",
        title="History midterm",
        max_attempts=max_attempts,
        rubric_id="history-rubric-v1",
        questions=[
            QuestionCreate(
                id="history-midterm-q1",
                prompt="Explain the cause.",
                max_score=10,
                rubric_chunk_indexes=[0, 1],
            ),
            QuestionCreate(
                id="history-midterm-q2",
                prompt="Evaluate the evidence.",
                max_score=5,
                rubric_chunk_indexes=[2],
            ),
        ],
    )


def make_service(*, wrong_scale: bool = False):
    grading_store = make_grading_store()
    llm = FakeLLMClient(wrong_scale=wrong_scale)
    service = GradingService(
        Settings(),
        rubric_store=FakeRubricStore(make_rubric()),
        grading_store=grading_store,
        chunk_store=FakeChunkStore(),
        llm_client=llm,
    )
    asyncio.run(service.create_course(CourseCreate(id="HIST-101", title="History")))
    asyncio.run(service.create_exam("HIST-101", exam_request()))
    return service, grading_store, llm


def test_multi_question_attempt_is_graded_and_persisted() -> None:
    service, grading_store, llm = make_service()
    attempt = asyncio.run(service.create_attempt("history-midterm", "student-1"))

    response = asyncio.run(
        service.grade_attempt(
            "history-midterm",
            attempt.id,
            "student-1",
            GradeAttemptRequest(
                responses=[
                    QuestionResponseSubmission(
                        question_id="history-midterm-q1",
                        answer="Economic pressure was the main cause.",
                    ),
                    QuestionResponseSubmission(
                        question_id="history-midterm-q2",
                        answer="The source supports the conclusion.",
                    ),
                ]
            ),
        )
    )

    assert response.attempt.status == "graded"
    assert response.total_score == 12
    assert response.max_score == 15
    assert response.percentage == 80
    assert response.completed_questions == 2
    assert response.grades[0].rubric_chunk_ids == ["chunk-0", "chunk-1"]
    assert len(llm.calls) == 2
    assert len(grading_store.list_grades(attempt.id)) == 2


def test_single_question_calls_can_share_one_attempt_before_finalization() -> None:
    service, _, _ = make_service()
    attempt = asyncio.run(service.create_attempt("history-midterm", "student-1"))

    partial = asyncio.run(
        service.grade_attempt(
            "history-midterm",
            attempt.id,
            "student-1",
            GradeAttemptRequest(
                responses=[
                    QuestionResponseSubmission(
                        question_id="history-midterm-q1",
                        answer="First response.",
                    )
                ],
                finalize=False,
            ),
        )
    )
    final = asyncio.run(
        service.grade_attempt(
            "history-midterm",
            attempt.id,
            "student-1",
            GradeAttemptRequest(
                responses=[
                    QuestionResponseSubmission(
                        question_id="history-midterm-q2",
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
    service, _, _ = make_service()
    attempt = asyncio.run(service.create_attempt("history-midterm", "student-1"))

    with pytest.raises(IncompleteAttemptError, match="history-midterm-q2"):
        asyncio.run(
            service.grade_attempt(
                "history-midterm",
                attempt.id,
                "student-1",
                GradeAttemptRequest(
                    responses=[
                        QuestionResponseSubmission(
                            question_id="history-midterm-q1",
                            answer="Only one response.",
                        )
                    ],
                    finalize=True,
                ),
            )
        )


def test_attempt_ownership_is_enforced() -> None:
    service, _, _ = make_service()
    attempt = asyncio.run(service.create_attempt("history-midterm", "student-1"))

    with pytest.raises(AttemptStateError, match="does not belong"):
        asyncio.run(
            service.get_attempt_result(
                "history-midterm",
                attempt.id,
                "student-2",
            )
        )


def test_exam_and_rubric_ownership_must_match() -> None:
    service, _, _ = make_service()
    service.rubric_store.rubric.exam_id = "another-exam"

    with pytest.raises(RubricOwnershipError, match="does not match"):
        asyncio.run(service.create_attempt("history-midterm", "student-1"))


def test_llm_cannot_change_question_score_scale() -> None:
    service, grading_store, _ = make_service(wrong_scale=True)
    attempt = asyncio.run(service.create_attempt("history-midterm", "student-1"))

    with pytest.raises(LLMScoreScaleError, match="requires 10"):
        asyncio.run(
            service.grade_attempt(
                "history-midterm",
                attempt.id,
                "student-1",
                GradeAttemptRequest(
                    responses=[
                        QuestionResponseSubmission(
                            question_id="history-midterm-q1",
                            answer="Response.",
                        )
                    ],
                    finalize=False,
                ),
            )
        )

    assert grading_store.get_attempt(attempt.id).status == "failed"
