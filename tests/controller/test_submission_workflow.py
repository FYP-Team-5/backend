import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.grading_repository import grade_changes
from app.dto import CriteriaGradingResult


def student(client, number):
    payload = {
        "email": f"student{number}@example.edu",
        "full_name": f"Student {number}",
        "student_number": f"S{number}",
        "password": "student-password-long-enough",
    }
    profile = client.post("/api/v1/auth/register/student", json=payload).json()
    token = client.post(
        "/api/v1/auth/login",
        json={"email": payload["email"], "password": payload["password"]},
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}, profile["id"]


def make_test(client, instructor_headers, **kwargs):
    course = client.post(
        "/api/v1/courses",
        json={"course_code": "CS", "course_name": "Computer Science"},
        headers=instructor_headers,
    ).json()
    payload = {
        "test_name": "Quiz",
        "ends_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        "max_attempts": 4,
        "questions": [
            {
                "prompt": "Explain",
                "max_score": 4,
                "score_increment": 1,
                "rubric": {"criteria": [{"description": "Correct", "score": 4}]},
            }
        ],
        **kwargs,
    }
    response = client.post(
        f"/api/v1/courses/{course['id']}/tests",
        json=payload,
        headers=instructor_headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def submit(client, test, headers):
    attempt = client.post(f"/api/v1/tests/{test['id']}/attempts", headers=headers)
    assert attempt.status_code == 201, attempt.text
    response = client.post(
        f"/api/v1/tests/{test['id']}/attempts/{attempt.json()['id']}/grade",
        headers=headers,
        json={
            "responses": [
                {"question_id": test["questions"][0]["id"], "answer": "Saved answer"}
            ]
        },
    )
    assert response.status_code == 200, response.text
    return attempt.json()["id"]


def wait_for_result(client, test_id, attempt_id, headers):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(
            f"/api/v1/tests/{test_id}/attempts/{attempt_id}", headers=headers
        )
        assert response.status_code == 200, response.text
        if response.json()["attempt"]["status"] in {"graded", "failed"}:
            return response.json()
        time.sleep(0.01)
    raise AssertionError("Grading did not finish")


def test_student_can_leave_return_and_see_instructor_grade_changes(
    client: TestClient, instructor_headers, llm_client, grading_repository
):
    llm_client.grade.return_value = CriteriaGradingResult(
        score=2, feedback="Automated feedback"
    )
    headers, user_id = student(client, 1)
    other, _ = student(client, 2)
    test = make_test(client, instructor_headers)
    attempt_id = submit(client, test, headers)
    # Reading courses and returning does not cancel the server worker.
    assert client.get("/api/v1/courses").status_code == 200
    result = wait_for_result(client, test["id"], attempt_id, headers)
    assert result["attempt"]["status"] == "graded"
    assert (
        client.get(f"/api/v1/tests/{test['id']}/attempts", headers=headers).json()[0][
            "id"
        ]
        == attempt_id
    )
    assert (
        client.get(
            f"/api/v1/tests/{test['id']}/attempts/{attempt_id}", headers=other
        ).status_code
        == 403
    )
    review_url = f"/api/v1/tests/{test['id']}/submissions/{attempt_id}"
    assert client.get(review_url, headers=headers).status_code == 403
    assert client.get(review_url).status_code == 401
    reviewed = client.get(review_url, headers=instructor_headers).json()
    assert reviewed["can_edit"]
    changed = client.patch(
        review_url + "/grades",
        headers=instructor_headers,
        json={
            "revision": reviewed["attempt"]["revision"],
            "responses": [
                {
                    "response_id": reviewed["responses"][0]["id"],
                    "score": 4,
                    "feedback": "Instructor feedback",
                }
            ],
        },
    )
    assert changed.status_code == 200, changed.text
    reopened = client.get(
        f"/api/v1/tests/{test['id']}/attempts/{attempt_id}", headers=headers
    ).json()
    assert reopened["total_score"] == 4
    assert reopened["percentage"] == 100
    assert reopened["responses"][0]["original_score"] == 2
    assert reopened["responses"][0]["feedback"] == "Instructor feedback"
    with grading_repository.engine.connect() as connection:
        audit = connection.execute(select(grade_changes)).mappings().one()
    assert audit["old_score"] == 2 and audit["new_score"] == 4
    assert audit["editor_id"] != user_id


def test_latest_submission_listing_includes_students_without_submissions(
    client, instructor_headers, llm_client
):
    llm_client.grade.return_value = CriteriaGradingResult(score=2, feedback="Feedback")
    headers, user_id = student(client, 1)
    _, other_id = student(client, 2)
    test = make_test(client, instructor_headers)
    attempt_id = submit(client, test, headers)
    wait_for_result(client, test["id"], attempt_id, headers)
    # An unsubmitted newer attempt must not replace the latest submission.
    client.post(f"/api/v1/tests/{test['id']}/attempts", headers=headers)
    url = f"/api/v1/tests/{test['id']}/submissions"
    assert client.get(url, headers=headers).status_code == 403
    rows = client.get(url, headers=instructor_headers).json()
    by_user = {row["user_id"]: row for row in rows}
    assert by_user[user_id]["latest_submission"]["id"] == attempt_id
    assert by_user[user_id]["total_score"] == 2
    assert by_user[other_id]["latest_submission"] is None
    assert by_user[other_id]["latest_graded_submission"] is None


def test_closure_rejects_new_answers_but_pending_grading_finishes(
    client, instructor_headers, llm_client
):
    release = threading.Event()

    async def delayed_grade(**kwargs):
        while not release.is_set():
            await asyncio.sleep(0.01)
        return CriteriaGradingResult(score=2, feedback="Feedback")

    llm_client.grade.side_effect = delayed_grade
    headers, _ = student(client, 1)
    other_headers, _ = student(client, 2)
    test = make_test(client, instructor_headers)
    attempt_id = submit(client, test, headers)
    other_attempt = client.post(
        f"/api/v1/tests/{test['id']}/attempts", headers=other_headers
    ).json()
    base = f"/api/v1/tests/{test['id']}"
    try:
        assert client.post(base + "/end", headers=headers).status_code == 403
        closed = client.post(base + "/end", headers=instructor_headers)
        assert closed.status_code == 200
        assert closed.json()["is_closed"]
        assert client.post(base + "/attempts", headers=headers).status_code == 409
        blocked = client.post(
            base + f"/attempts/{other_attempt['id']}/grade",
            headers=other_headers,
            json={
                "responses": [
                    {"question_id": test["questions"][0]["id"], "answer": "Late"}
                ]
            },
        )
        assert blocked.status_code == 409
        pending = client.get(base + f"/attempts/{attempt_id}", headers=headers).json()
        assert pending["attempt"]["status"] == "grading"
        assert pending["responses"][0]["answer"] == "Saved answer"
    finally:
        release.set()
    assert (
        wait_for_result(client, test["id"], attempt_id, headers)["attempt"]["status"]
        == "graded"
    )


def test_deadline_creation_and_updates_are_validated_and_role_protected(
    client, instructor_headers
):
    headers, _ = student(client, 1)
    test = make_test(client, instructor_headers)
    url = f"/api/v1/tests/{test['id']}/deadline"
    future = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    assert (
        client.patch(url, headers=headers, json={"ends_at": future}).status_code == 403
    )
    assert (
        client.patch(
            url, headers=instructor_headers, json={"ends_at": "2020-01-01T00:00:00Z"}
        ).status_code
        == 422
    )
    assert (
        client.patch(
            url, headers=instructor_headers, json={"ends_at": "2030-01-01T00:00:00"}
        ).status_code
        == 422
    )
    assert (
        client.patch(
            url, headers=instructor_headers, json={"ends_at": future}
        ).status_code
        == 200
    )
    course_id = test["course_id"]
    payload = {
        "test_name": "Missing deadline",
        "questions": [{"prompt": "Q", "max_score": 1, "score_increment": 1}],
    }
    assert (
        client.post(
            f"/api/v1/courses/{course_id}/tests",
            headers=instructor_headers,
            json=payload,
        ).status_code
        == 422
    )


def test_student_test_and_result_views_hide_instructor_material(
    client, instructor_headers, grading_repository, llm_client
):
    from app.dto import ExampleCreate

    llm_client.grade.return_value = CriteriaGradingResult(score=2, feedback="Feedback")
    headers, _ = student(client, 1)
    test = make_test(client, instructor_headers)
    question = test["questions"][0]
    grading_repository.set_question_examples(
        test["id"],
        question["id"],
        [
            ExampleCreate(
                band="excellent", example_answer="Instructor exemplar", score=4
            )
        ],
    )
    grading_repository.set_grading_method(test["id"], question["id"], "rubric")
    from app.dto import CriteriaCreate, RubricCreate

    grading_repository.set_question_rubric(
        test["id"],
        question["id"],
        RubricCreate(
            model_answer="Instructor answer key",
            criteria=[CriteriaCreate(description="Secret criterion", score=4)],
        ),
    )
    base = f"/api/v1/tests/{test['id']}"
    assert client.get(base).status_code == 401
    instructor_view = client.get(base, headers=instructor_headers).json()
    assert instructor_view["questions"][0]["model_answer"] == "Instructor answer key"
    assert instructor_view["questions"][0]["rubric"] is not None
    student_view = client.get(base, headers=headers).json()
    listed = client.get(
        f"/api/v1/courses/{test['course_id']}/tests", headers=headers
    ).json()
    attempt_id = submit(client, test, headers)
    result = wait_for_result(client, test["id"], attempt_id, headers)
    for questions in [
        student_view["questions"],
        listed[0]["questions"],
        result["questions"],
    ]:
        assert questions[0]["model_answer"] is None
        assert questions[0]["rubric"] is None
        assert questions[0]["examples"] == []
    assert result["responses"][0]["answer"] == "Saved answer"
    assert result["responses"][0]["feedback"] == "Feedback"
