from fastapi.testclient import TestClient

STUDENT = {
    "email": "student@example.edu",
    "full_name": "Student One",
    "student_number": "S0001",
    "password": "a-secure-student-password",
}
STAFF = {
    "email": "staff@example.edu",
    "full_name": "Staff One",
    "staff_number": "E0001",
    "password": "a-secure-staff-password",
}


def _login(client: TestClient, email: str, password: str) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def test_health_and_openapi(client: TestClient) -> None:
    response = client.get("/health")
    health = response.json()
    assert health["status"] == "ok"
    assert health["postgres"] == "ok"
    assert health["qdrant"] == "ok"
    assert health["llm"] == "ok"

    schema = client.get("/openapi.json").json()
    assert "/api/v1/auth/login" in schema["paths"]
    assert "/api/v1/users/me" in schema["paths"]


def test_student_registration_login_and_profile(client: TestClient) -> None:
    registration = client.post("/api/v1/auth/register/student", json=STUDENT)

    assert registration.status_code == 201
    assert registration.json()["role"] == "student"
    assert "password" not in registration.json()

    token = _login(client, STUDENT["email"], STUDENT["password"])
    profile = client.get(
        "/api/v1/users/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert profile.status_code == 200
    assert profile.json()["student_number"] == "S0001"


def test_staff_administration_flow(client: TestClient) -> None:
    student = client.post("/api/v1/auth/register/student", json=STUDENT).json()
    denied = client.post("/api/v1/auth/register/staff", json=STAFF)
    assert denied.status_code == 403

    created = client.post(
        "/api/v1/auth/register/staff",
        json=STAFF,
        headers={"X-Staff-Registration-Key": "test-staff-registration-key"},
    )
    assert created.status_code == 201
    token = _login(client, STAFF["email"], STAFF["password"])
    headers = {"Authorization": f"Bearer {token}"}

    users = client.get("/api/v1/users?role=student", headers=headers)
    assert users.status_code == 200
    assert [user["id"] for user in users.json()] == [student["id"]]

    disabled = client.patch(
        f"/api/v1/users/{student['id']}/status",
        json={"active": False},
        headers=headers,
    )
    assert disabled.status_code == 200
    assert disabled.json()["active"] is False
    failed_login = client.post(
        "/api/v1/auth/login",
        json={"email": STUDENT["email"], "password": STUDENT["password"]},
    )
    assert failed_login.status_code == 401


def test_bearer_and_role_protection(client: TestClient) -> None:
    assert client.get("/api/v1/users/me").status_code == 401
    client.post("/api/v1/auth/register/student", json=STUDENT)
    token = _login(client, STUDENT["email"], STUDENT["password"])

    response = client.get(
        "/api/v1/users",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


def test_duplicate_and_validation_errors(client: TestClient) -> None:
    assert client.post("/api/v1/auth/register/student", json=STUDENT).status_code == 201
    assert client.post("/api/v1/auth/register/student", json=STUDENT).status_code == 409

    weak = {**STUDENT, "email": "invalid", "password": "short"}
    assert client.post("/api/v1/auth/register/student", json=weak).status_code == 422
