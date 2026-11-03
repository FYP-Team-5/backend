import asyncio

from fastapi.testclient import TestClient

from app.dto import StudentRegistration
from app.main import create_app


def test_startup_accounts_can_log_in_and_survive_restart(
    settings, identity_service, grading_service, dataset_service
):
    settings.seed_demo_users = True
    app = create_app(
        settings=settings,
        identity_service=identity_service,
        grading_service=grading_service,
        dataset_service=dataset_service,
    )
    accounts = (
        ("student@demo.example.com", "StudentDemo123!", "student"),
        ("instructor@demo.example.com", "InstructorDemo123!", "instructor"),
    )
    original_users = None
    for _ in range(2):
        with TestClient(app) as client:
            for email, password, role in accounts:
                response = client.post(
                    "/api/v1/auth/login", json={"email": email, "password": password}
                )
                assert response.status_code == 200
                assert response.json()["user"]["role"] == role
                profile = client.get(
                    "/api/v1/users/me",
                    headers={"Authorization": f"Bearer {response.json()['access_token']}"},
                )
                assert profile.status_code == 200
                assert profile.json()["email"] == email
                _, stored_hash = identity_service.repository.get_by_email(email)
                assert stored_hash != password
                assert identity_service.password_hasher.verify(password, stored_hash)
            users = identity_service.repository.list()
            assert len(users) == 2
            if original_users is not None:
                assert users == original_users
            original_users = users


def test_startup_preserves_existing_account_credentials_and_inactive_status(
    settings, identity_service
):
    existing = asyncio.run(
        identity_service.register_student(
            StudentRegistration(
                email="student@demo.example.com",
                full_name="Existing Student",
                student_number="EXISTING-STUDENT",
                password="existing-student-password",
            )
        )
    )
    identity_service.repository.set_active(existing.id, False)
    original = identity_service.repository.get(existing.id)
    settings.seed_demo_users = True

    asyncio.run(identity_service.initialize())

    assert identity_service.repository.get(existing.id) == original
    assert len(identity_service.repository.list()) == 2
    _, password_hash = original
    assert identity_service.password_hasher.verify(
        "existing-student-password", password_hash
    )
    assert not identity_service.password_hasher.verify("StudentDemo123!", password_hash)


def test_startup_seeding_can_be_disabled(client, repository):
    assert repository.list() == []
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "instructor@demo.example.com", "password": "InstructorDemo123!"},
    )
    assert response.status_code == 401
