"""Keep instructor answer keys out of student-facing assessment responses."""

from app.model import Question, Test


def student_questions(questions: list[Question]) -> list[Question]:
    return [
        question.model_copy(
            update={
                "model_answer": None,
                "rubric": None,
                "examples": [],
                "grading_method": None,
            }
        )
        for question in questions
    ]


def test_view(test: Test, role: str) -> Test:
    if role == "instructor":
        return test
    return test.model_copy(update={"questions": student_questions(test.questions)})
