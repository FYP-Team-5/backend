from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

STUDENT = {
    "email": "student@example.edu",
    "full_name": "Student One",
    "student_number": "S0001",
    "password": "a-secure-student-password",
}
OTHER_STUDENT = {
    "email": "other@example.edu",
    "full_name": "Other Student",
    "student_number": "S0002",
    "password": "another-secure-password",
}


def _login(client: TestClient, email: str, password: str) -> str:
    response = client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def _create_test_with_rubric(client: TestClient, instructor_headers: dict) -> str:
    course_id = client.post(
        "/api/v1/courses",
        headers=instructor_headers,
        json={"course_code": "CS-101", "course_name": "Intro CS"},
    ).json()["id"]
    test = client.post(
        f"/api/v1/courses/{course_id}/tests",
        headers=instructor_headers,
        json={
            "test_name": "Quiz 1",
            "ends_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            "max_attempts": 1,
            "questions": [
                {
                    "prompt": "What is a prototype?",
                    "max_score": 1,
                    "score_increment": 1,
                    "rubric": {
                        "criteria": [
                            {"description": "Mentions simulation", "score": 1}
                        ],
                    },
                }
            ],
        },
    ).json()
    return test["id"]


def test_create_attempt_requires_a_bearer_token(
    client: TestClient, instructor_headers: dict
) -> None:
    test_id = _create_test_with_rubric(client, instructor_headers)

    response = client.post(f"/api/v1/tests/{test_id}/attempts")

    assert response.status_code == 401


def test_create_attempt_ignores_a_caller_supplied_user_id(
    client: TestClient, instructor_headers: dict
) -> None:
    """An attempt always belongs to whoever the bearer token authenticates
    as - a stale/forged X-User-ID header must not override that, since
    trusting it would let one student read or grade another's attempts.
    """
    test_id = _create_test_with_rubric(client, instructor_headers)
    client.post("/api/v1/auth/register/student", json=STUDENT)
    student_id = client.post(
        "/api/v1/auth/register/student", json=OTHER_STUDENT
    ).json()["id"]
    token = _login(client, STUDENT["email"], STUDENT["password"])

    response = client.post(
        f"/api/v1/tests/{test_id}/attempts",
        headers={"Authorization": f"Bearer {token}", "X-User-ID": student_id},
    )

    assert response.status_code == 201
    assert response.json()["user_id"] != student_id
