"""Test availability is determined by the [starts_at, ends_at) window."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient


def student(client: TestClient, number: int) -> dict[str, str]:
    payload = {
        "email": f"window-student{number}@example.edu",
        "full_name": f"Window Student {number}",
        "student_number": f"W{number}",
        "password": "student-password-long-enough",
    }
    registered = client.post("/api/v1/auth/register/student", json=payload)
    assert registered.status_code == 201, registered.text
    token = client.post(
        "/api/v1/auth/login",
        json={"email": payload["email"], "password": payload["password"]},
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _create_course(client: TestClient, instructor_headers: dict) -> str:
    response = client.post(
        "/api/v1/courses",
        headers=instructor_headers,
        json={"course_code": "WIN-101", "course_name": "Window CS"},
    )
    assert response.status_code == 201
    return response.json()["id"]


def _make_test(
    client: TestClient,
    instructor_headers: dict,
    *,
    starts_at: str | None = None,
    ends_at: str,
) -> dict:
    payload = {
        "test_name": "Window Quiz",
        "max_attempts": 2,
        "ends_at": ends_at,
        "questions": [
            {
                "prompt": "Explain",
                "max_score": 4,
                "score_increment": 1,
                "rubric": {"criteria": [{"description": "Correct", "score": 4}]},
            }
        ],
    }
    if starts_at is not None:
        payload["starts_at"] = starts_at
    response = client.post(
        f"/api/v1/courses/{_create_course(client, instructor_headers)}/tests",
        json=payload,
        headers=instructor_headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _attempt(client: TestClient, test_id: str, headers: dict):
    return client.post(f"/api/v1/tests/{test_id}/attempts", headers=headers)


def test_without_start_date_the_test_is_open_immediately(
    client: TestClient, instructor_headers: dict
) -> None:
    headers = student(client, 1)
    test = _make_test(
        client, instructor_headers, ends_at=(datetime.now(UTC) + timedelta(days=1)).isoformat()
    )
    assert test["is_closed"] is False
    assert _attempt(client, test["id"], headers).status_code == 201


def test_future_start_date_keeps_the_test_closed(
    client: TestClient, instructor_headers: dict
) -> None:
    headers = student(client, 2)
    now = datetime.now(UTC)
    test = _make_test(
        client,
        instructor_headers,
        starts_at=(now + timedelta(days=1)).isoformat(),
        ends_at=(now + timedelta(days=2)).isoformat(),
    )
    assert test["is_closed"] is True
    assert test["starts_at"] is not None
    rejected = _attempt(client, test["id"], headers)
    assert rejected.status_code == 409
    assert "not opened" in rejected.json()["detail"]
    assert client.get(f"/api/v1/tests/{test['id']}", headers=headers).json()["is_closed"]


def test_start_after_end_is_rejected(
    client: TestClient, instructor_headers: dict
) -> None:
    now = datetime.now(UTC)
    response = client.post(
        f"/api/v1/courses/{_create_course(client, instructor_headers)}/tests",
        headers=instructor_headers,
        json={
            "test_name": "Backwards Quiz",
            "starts_at": (now + timedelta(days=2)).isoformat(),
            "ends_at": (now + timedelta(days=1)).isoformat(),
            "questions": [
                {
                    "prompt": "Explain",
                    "max_score": 4,
                    "score_increment": 1,
                    "rubric": {"criteria": [{"description": "Correct", "score": 4}]},
                }
            ],
        },
    )
    assert response.status_code == 422


def test_upcoming_test_deadline_stays_editable_and_ordered(
    client: TestClient, instructor_headers: dict
) -> None:
    now = datetime.now(UTC)
    test = _make_test(
        client,
        instructor_headers,
        starts_at=(now + timedelta(days=1)).isoformat(),
        ends_at=(now + timedelta(days=2)).isoformat(),
    )
    deadline_url = f"/api/v1/tests/{test['id']}/deadline"
    before_start = client.patch(
        deadline_url,
        headers=instructor_headers,
        json={"ends_at": (now + timedelta(hours=12)).isoformat()},
    )
    assert before_start.status_code == 409
    assert "after the start" in before_start.json()["detail"]
    extended = client.patch(
        deadline_url,
        headers=instructor_headers,
        json={"ends_at": (now + timedelta(days=3)).isoformat()},
    )
    assert extended.status_code == 200, extended.text
    assert extended.json()["is_closed"] is True  # still upcoming
    assert extended.json()["starts_at"] == test["starts_at"]


def test_csv_upload_accepts_a_start_date(
    client: TestClient, instructor_headers: dict
) -> None:
    now = datetime.now(UTC)
    response = client.post(
        f"/api/v1/courses/{_create_course(client, instructor_headers)}/tests/csv",
        data={
            "test_name": "Quiz 1",
            "max_attempts": "1",
            "starts_at": (now + timedelta(days=1)).isoformat(),
            "ends_at": (now + timedelta(days=2)).isoformat(),
        },
        headers=instructor_headers,
        files={
            "file": (
                "questions.csv",
                "id,prompt,max_score,score_increment\n1.1,Explain,1,1\n",
                "text/csv",
            )
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["is_closed"] is True
