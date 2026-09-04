import pytest
from sqlalchemy import create_engine

from app.db import AttemptLimitExceededError, PostgresGradingRepository
from app.dto import CriteriaCreate, QuestionCreate, RubricCreate, TestCreate


def make_repository() -> PostgresGradingRepository:
    repository = PostgresGradingRepository(
        engine=create_engine("sqlite+pysqlite:///:memory:")
    )
    repository.initialize()
    return repository


def make_test_request(*, max_attempts: int = 1) -> TestCreate:
    return TestCreate(
        test_name="History midterm",
        max_attempts=max_attempts,
        questions=[
            QuestionCreate(
                prompt="Explain the primary cause.",
                max_score=10,
                score_increment=0.5,
                rubric=RubricCreate(
                    criteria=[
                        CriteriaCreate(description="Identifies the primary cause.", score=6),
                        CriteriaCreate(description="Supports the claim with evidence.", score=4),
                    ]
                ),
            ),
        ],
    )


def test_catalog_attempt_and_grade_round_trip() -> None:
    repository = make_repository()
    course = repository.create_course("HIST-101", "History")
    test = repository.create_test(course.id, make_test_request(max_attempts=2))

    assert test.course_id == course.id
    assert [question.position for question in test.questions] == [0]
    question = test.questions[0]
    assert len(question.rubric.criteria) == 2

    attempt = repository.create_attempt(test_id=test.id, user_id="student-1")
    response_id = repository.save_response(
        attempt.id,
        question.id,
        "Economic pressure was the main cause.",
    )
    repository.save_response_grade(
        response_id=response_id,
        score=8,
        feedback="Good explanation.",
        criteria_met_results=[
            {"criteria_id": question.rubric.criteria[0].id, "is_met": True},
            {"criteria_id": question.rubric.criteria[1].id, "is_met": False},
        ],
    )

    responses = repository.list_responses(attempt.id)
    completed = repository.mark_attempt_graded(attempt.id)

    assert responses[0].question_id == question.id
    assert responses[0].score == 8
    assert {item.criteria_id for item in responses[0].criteria_met} == {
        question.rubric.criteria[0].id,
        question.rubric.criteria[1].id,
    }
    assert completed.status == "graded"


def test_attempt_limit_is_enforced_per_user_and_test() -> None:
    repository = make_repository()
    course = repository.create_course("HIST-101", "History")
    test = repository.create_test(course.id, make_test_request(max_attempts=1))
    repository.create_attempt(test_id=test.id, user_id="student-1")

    with pytest.raises(AttemptLimitExceededError, match="allows 1 attempt"):
        repository.create_attempt(test_id=test.id, user_id="student-1")

    other_student = repository.create_attempt(test_id=test.id, user_id="student-2")
    assert other_student.attempt_number == 1


def test_each_question_gets_its_own_rubric() -> None:
    repository = make_repository()
    course = repository.create_course("HIST-101", "History")
    request = TestCreate(
        test_name="History midterm",
        max_attempts=1,
        questions=[
            QuestionCreate(
                prompt="Explain the primary cause.",
                max_score=10,
                score_increment=0.5,
                rubric=RubricCreate(criteria=[CriteriaCreate(description="Accuracy", score=10)]),
            ),
            QuestionCreate(
                prompt="Evaluate the evidence.",
                max_score=5,
                score_increment=0.5,
                rubric=RubricCreate(criteria=[CriteriaCreate(description="Evidence", score=5)]),
            ),
        ],
    )

    test = repository.create_test(course.id, request)

    assert test.questions[0].rubric.id != test.questions[1].rubric.id
    assert [item.description for item in test.questions[0].rubric.criteria] == ["Accuracy"]
    assert [item.description for item in test.questions[1].rubric.criteria] == ["Evidence"]


def test_question_can_be_created_without_a_rubric_and_assigned_one_later() -> None:
    repository = make_repository()
    course = repository.create_course("HIST-101", "History")
    request = TestCreate(
        test_name="History midterm",
        max_attempts=1,
        questions=[
            QuestionCreate(prompt="Explain the primary cause.", max_score=10, score_increment=0.5),
        ],
    )

    test = repository.create_test(course.id, request)
    question = test.questions[0]
    assert question.rubric is None

    updated_question = repository.set_question_rubric(
        test.id,
        question.id,
        RubricCreate(criteria=[CriteriaCreate(description="Accuracy", score=10)]),
    )

    assert updated_question.rubric is not None
    assert [item.description for item in updated_question.rubric.criteria] == ["Accuracy"]
    refetched = repository.get_test(test.id)
    assert refetched.questions[0].rubric.criteria[0].description == "Accuracy"


def test_setting_a_new_rubric_replaces_the_old_one() -> None:
    repository = make_repository()
    course = repository.create_course("HIST-101", "History")
    test = repository.create_test(course.id, make_test_request())
    question = test.questions[0]
    old_rubric_id = question.rubric.id

    updated_question = repository.set_question_rubric(
        test.id,
        question.id,
        RubricCreate(criteria=[CriteriaCreate(description="Replacement criterion", score=10)]),
    )

    assert updated_question.rubric.id != old_rubric_id
    assert [item.description for item in updated_question.rubric.criteria] == [
        "Replacement criterion"
    ]


def test_catalog_lists_courses_tests_and_attempts() -> None:
    repository = make_repository()
    course = repository.create_course("HIST-101", "History")
    test = repository.create_test(course.id, make_test_request(max_attempts=2))
    attempt = repository.create_attempt(test_id=test.id, user_id="student-1")

    assert [item.id for item in repository.list_courses()] == [course.id]
    assert [item.id for item in repository.list_tests(course.id)] == [test.id]
    assert repository.list_attempts(test.id, "student-1") == [attempt]
