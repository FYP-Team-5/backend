import csv
import io
import time
from pathlib import Path

import pytest

from app.dto import CriteriaGradingResult
from tests.controller.test_submission_workflow import (
    make_test,
    student,
    submit,
    wait_for_result,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/training_dataset.csv"


def wait_version(client, headers, version_id):
    until = time.monotonic() + 3
    while time.monotonic() < until:
        response = client.get(
            f"/api/v1/training-dataset/versions/{version_id}", headers=headers
        )
        assert response.status_code == 200, response.text
        if response.json()["status"] in {"ready", "failed"}:
            return response.json()
        time.sleep(0.01)
    raise AssertionError("CSV publication did not settle")


def upload_csv(client, headers, body=None, key="upload-one"):
    response = client.post(
        "/api/v1/training-dataset/versions",
        headers={**headers, "Idempotency-Key": key},
        files={
            "file": (
                "training.csv",
                body if body is not None else FIXTURE.read_bytes(),
                "text/csv",
            )
        },
    )
    assert response.status_code == 202, response.text
    return response.json()


def test_instructor_csv_upload_history_and_download(client, instructor_headers):
    version = upload_csv(client, instructor_headers)
    same = upload_csv(client, instructor_headers)
    assert version["id"] == same["id"]
    published = wait_version(client, instructor_headers, version["id"])
    assert published["status"] == "ready"
    assert published["row_count"] == 3
    history = client.get(
        "/api/v1/training-dataset/versions", headers=instructor_headers
    ).json()
    assert len(history) == 1 and history[0]["is_active"]
    assert "object_key" not in history[0] and "input_seed" not in history[0]
    response = client.get(
        "/api/v1/training-dataset/download", headers=instructor_headers
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"].endswith('v000001.csv"')
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == 3 and rows[2]["student_answer"] == ""
    historical = client.get(
        f"/api/v1/training-dataset/versions/{version['id']}/download",
        headers=instructor_headers,
    )
    assert historical.content == response.content


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/training-dataset/status"),
        ("GET", "/training-dataset/versions"),
        ("GET", "/training-dataset/versions/missing"),
        ("GET", "/training-dataset/download"),
        ("GET", "/training-dataset/versions/missing/download"),
        ("POST", "/training-dataset/versions/missing/retry"),
        ("GET", "/tests/missing/training-export"),
        ("POST", "/tests/missing/training-export/retry"),
    ],
)
def test_dataset_endpoints_require_instructor(client, method, path):
    headers, _ = student(client, 1)
    assert client.request(method, "/api/v1" + path, headers=headers).status_code == 403
    assert client.request(method, "/api/v1" + path).status_code == 401


def test_student_cannot_upload_csv(client):
    headers, _ = student(client, 1)
    response = client.post(
        "/api/v1/training-dataset/versions",
        headers=headers,
        files={"file": ("training.csv", FIXTURE.read_bytes(), "text/csv")},
    )
    assert response.status_code == 403


def test_upload_validation_size_limit_and_idempotency_conflict(
    client, instructor_headers, settings
):
    assert (
        client.post(
            "/api/v1/training-dataset/versions",
            headers=instructor_headers,
            files={"file": ("invalid.csv", b"wrong,header\n", "text/csv")},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/training-dataset/versions",
            headers=instructor_headers,
            files={"file": ("invalid.jsonl", b"{}", "text/csv")},
        ).status_code
        == 422
    )
    upload_csv(client, instructor_headers, key="fixed-key")
    conflict = client.post(
        "/api/v1/training-dataset/versions",
        headers={**instructor_headers, "Idempotency-Key": "fixed-key"},
        files={
            "file": (
                "training.csv",
                FIXTURE.read_bytes().replace(
                    b"An unrelated answer.", b"Different answer."
                ),
                "text/csv",
            )
        },
    )
    assert conflict.status_code == 409
    settings.dataset_upload_max_bytes = 20
    assert (
        client.post(
            "/api/v1/training-dataset/versions",
            headers=instructor_headers,
            files={"file": ("large.csv", FIXTURE.read_bytes(), "text/csv")},
        ).status_code
        == 413
    )


def test_criterion_corrections_enable_automatic_export_after_closure(
    client, instructor_headers, llm_client
):
    # The model provided no per-criterion labels; the instructor can supply them.
    llm_client.grade.return_value = CriteriaGradingResult(score=2, feedback="Feedback")
    headers, _ = student(client, 1)
    test = make_test(client, instructor_headers)
    attempt_id = submit(client, test, headers)
    result = wait_for_result(client, test["id"], attempt_id, headers)
    review = f"/api/v1/tests/{test['id']}/submissions/{attempt_id}/grades"
    updated = client.patch(
        review,
        headers=instructor_headers,
        json={
            "revision": result["attempt"]["revision"],
            "responses": [
                {
                    "response_id": result["responses"][0]["id"],
                    "score": 4,
                    "feedback": "Reviewed",
                    "criterion_scores": [
                        {
                            "criteria_id": test["questions"][0]["rubric"]["criteria"][
                                0
                            ]["id"],
                            "score": 4,
                        }
                    ],
                }
            ],
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["responses"][0]["criterion_scores"][0]["score"] == 4
    client.post(f"/api/v1/tests/{test['id']}/end", headers=instructor_headers)
    until = time.monotonic() + 3
    while time.monotonic() < until:
        status = client.get(
            f"/api/v1/tests/{test['id']}/training-export", headers=instructor_headers
        ).json()
        if status["status"] == "completed":
            break
        time.sleep(0.02)
    assert status["status"] == "completed", status
    response = client.get(
        "/api/v1/training-dataset/download", headers=instructor_headers
    )
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == 1 and float(rows[0]["student_score"]) == 4


def test_storage_outage_does_not_fail_api_health_or_publish_partial_version(
    client, instructor_headers, dataset_storage, settings
):
    dataset_storage.fail = True
    settings.dataset_max_retries = 0
    version = upload_csv(client, instructor_headers)
    result = wait_version(client, instructor_headers, version["id"])
    assert result["status"] == "failed"
    assert client.get("/health").status_code == 200
    assert client.get("/health").json()["seaweedfs"] == "unavailable"
    assert (
        client.get(
            "/api/v1/training-dataset/download", headers=instructor_headers
        ).status_code
        == 404
    )
