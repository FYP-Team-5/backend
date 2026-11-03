from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient


def _create_course(client: TestClient, instructor_headers: dict) -> str:
    response = client.post(
        "/api/v1/courses",
        headers=instructor_headers,
        json={"course_code": "CS-101", "course_name": "Intro CS"},
    )
    assert response.status_code == 201
    return response.json()["id"]


def test_questions_csv_upload_creates_a_test(
    client: TestClient, instructor_headers: dict
) -> None:
    course_id = _create_course(client, instructor_headers)
    csv_content = (
        "id,prompt,max_score,score_increment\n"
        "1.1,What is the role of a prototype program?,1,1\n"
        "1.2,Why prototype early?,2,1\n"
    )

    response = client.post(
        f"/api/v1/courses/{course_id}/tests/csv",
        data={
            "test_name": "Quiz 1",
            "max_attempts": "1",
            "ends_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        },
        headers=instructor_headers,
        files={"file": ("questions.csv", csv_content, "text/csv")},
    )

    assert response.status_code == 201
    body = response.json()
    assert [question["external_id"] for question in body["questions"]] == ["1.1", "1.2"]
    assert all(question["rubric"] is None for question in body["questions"])


def test_criteria_csv_upload_attaches_rubric_and_model_answer(
    client: TestClient, instructor_headers: dict
) -> None:
    course_id = _create_course(client, instructor_headers)
    questions_csv = (
        "id,prompt,max_score,score_increment\n1.1,What is a prototype?,1,1\n"
    )
    test_id = client.post(
        f"/api/v1/courses/{course_id}/tests/csv",
        data={
            "test_name": "Quiz 1",
            "max_attempts": "1",
            "ends_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        },
        headers=instructor_headers,
        files={"file": ("questions.csv", questions_csv, "text/csv")},
    ).json()["id"]
    criteria_csv = (
        "id,criteria,criteria_max_score,model_answer\n"
        "1.1,Mentions simulating behaviour,1,To simulate the behaviour of the product.\n"
    )

    response = client.post(
        f"/api/v1/tests/{test_id}/criteria/csv",
        headers=instructor_headers,
        files={"file": ("criteria.csv", criteria_csv, "text/csv")},
    )

    assert response.status_code == 200
    question = response.json()["questions"][0]
    assert question["model_answer"] == "To simulate the behaviour of the product."
    assert [item["description"] for item in question["rubric"]["criteria"]] == [
        "Mentions simulating behaviour"
    ]


def test_criteria_csv_upload_rejects_unknown_question_id(
    client: TestClient, instructor_headers: dict
) -> None:
    course_id = _create_course(client, instructor_headers)
    questions_csv = (
        "id,prompt,max_score,score_increment\n1.1,What is a prototype?,1,1\n"
    )
    test_id = client.post(
        f"/api/v1/courses/{course_id}/tests/csv",
        data={
            "test_name": "Quiz 1",
            "max_attempts": "1",
            "ends_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        },
        headers=instructor_headers,
        files={"file": ("questions.csv", questions_csv, "text/csv")},
    ).json()["id"]
    criteria_csv = "id,criteria,criteria_max_score\n9.9,Some criterion,1\n"

    response = client.post(
        f"/api/v1/tests/{test_id}/criteria/csv",
        headers=instructor_headers,
        files={"file": ("criteria.csv", criteria_csv, "text/csv")},
    )

    assert response.status_code == 409
    assert "9.9" in response.json()["detail"]


def test_questions_csv_upload_rejects_malformed_csv(
    client: TestClient, instructor_headers: dict
) -> None:
    course_id = _create_course(client, instructor_headers)

    response = client.post(
        f"/api/v1/courses/{course_id}/tests/csv",
        data={
            "test_name": "Quiz 1",
            "max_attempts": "1",
            "ends_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        },
        headers=instructor_headers,
        files={
            "file": ("questions.csv", "id,prompt\n1.1,Missing columns\n", "text/csv")
        },
    )

    assert response.status_code == 422
