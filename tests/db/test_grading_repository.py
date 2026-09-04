import pytest
from sqlalchemy import create_engine

from app.db import AttemptLimitExceededError, PostgresGradingRepository
from app.dto import ExamCreate, QuestionCreate


def make_repository() -> PostgresGradingRepository:
    repository = PostgresGradingRepository(
        engine=create_engine("sqlite+pysqlite:///:memory:")
    )
    repository.initialize()
    return repository


def make_exam(max_attempts: int = 1) -> ExamCreate:
    return ExamCreate(
        id="history-midterm",
        title="History midterm",
        type="exam",
        max_attempts=max_attempts,
        rubric_id="history-midterm-rubric-v1",
        questions=[
            QuestionCreate(
                id="history-midterm-q1",
                prompt="Explain the primary cause.",
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


def test_catalog_attempt_and_grade_round_trip() -> None:
    repository = make_repository()
    repository.create_course("HIST-101", "History")
    exam = repository.create_exam("HIST-101", make_exam(max_attempts=2))

    assert exam.course_id == "HIST-101"
    assert [question.position for question in exam.questions] == [0, 1]

    attempt = repository.create_attempt(
        exam_id=exam.id,
        student_id="student-1",
        rubric_id=exam.rubric_id,
        rubric_version="1",
    )
    response_id = repository.save_response(
        attempt.id,
        "history-midterm-q1",
        "Economic pressure was the main cause.",
    )
    repository.save_grade(
        attempt_id=attempt.id,
        response_id=response_id,
        question_id="history-midterm-q1",
        score=8,
        max_score=10,
        feedback="Good explanation.",
        criteria=[{"criterion": "Accuracy", "score": 8, "max_score": 10}],
        rubric_id=exam.rubric_id,
        rubric_version="1",
        rubric_chunk_ids=["chunk-1", "chunk-2"],
        llm_model="grader",
        prompt_version="2.0",
    )

    grades = repository.list_grades(attempt.id)
    completed = repository.mark_attempt_graded(attempt.id)

    assert grades[0].question_id == "history-midterm-q1"
    assert grades[0].score == 8
    assert grades[0].rubric_chunk_ids == ["chunk-1", "chunk-2"]
    assert completed.status == "graded"


def test_attempt_limit_is_enforced_per_student_and_exam() -> None:
    repository = make_repository()
    repository.create_course("HIST-101", "History")
    exam = repository.create_exam("HIST-101", make_exam(max_attempts=1))
    repository.create_attempt(
        exam_id=exam.id,
        student_id="student-1",
        rubric_id=exam.rubric_id,
        rubric_version="1",
    )

    with pytest.raises(AttemptLimitExceededError, match="allows 1 attempt"):
        repository.create_attempt(
            exam_id=exam.id,
            student_id="student-1",
            rubric_id=exam.rubric_id,
            rubric_version="1",
        )

    other_student = repository.create_attempt(
        exam_id=exam.id,
        student_id="student-2",
        rubric_id=exam.rubric_id,
        rubric_version="1",
    )
    assert other_student.attempt_number == 1


def test_question_chunk_mapping_can_be_updated_after_rubric_processing() -> None:
    repository = make_repository()
    repository.create_course("HIST-101", "History")
    repository.create_exam("HIST-101", make_exam())

    question = repository.update_question_chunk_indexes(
        "history-midterm",
        "history-midterm-q1",
        [3, 4],
    )

    assert question.rubric_chunk_indexes == [3, 4]


def test_catalog_lists_rubric_activation_and_attempt_listing() -> None:
    repository = make_repository()
    repository.create_course("HIST-101", "History")
    exam = repository.create_exam("HIST-101", make_exam(max_attempts=2))

    updated = repository.update_exam_rubric(exam.id, "history-midterm-rubric-v2")
    attempt = repository.create_attempt(
        exam_id=exam.id,
        student_id="student-1",
        rubric_id=updated.rubric_id,
        rubric_version="2",
    )

    assert [course.id for course in repository.list_courses()] == ["HIST-101"]
    assert [item.id for item in repository.list_exams("HIST-101")] == [exam.id]
    assert updated.rubric_id == "history-midterm-rubric-v2"
    assert repository.list_attempts(exam.id, "student-1") == [attempt]
