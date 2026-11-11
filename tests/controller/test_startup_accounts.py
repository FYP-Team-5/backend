import asyncio

from fastapi.testclient import TestClient

from app.dto import StudentRegistration
from app.main import create_app
from tests.controller.test_training_dataset_api import wait_version


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
        ("student@example.com", "Password123!", "student"),
        ("student2@example.com", "Password123!", "student"),
        ("instructor@example.com", "Password123!", "instructor"),
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
            assert len(users) == 3
            if original_users is not None:
                assert users == original_users
            original_users = users
            headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
            versions = client.get(
                "/api/v1/training-dataset/versions", headers=headers
            ).json()
            assert len(versions) == 1
            published = wait_version(client, headers, versions[0]["id"])
            assert published["status"] == "ready"
            assert published["is_active"]
            assert published["filename"] == "single_row.csv"
            assert published["row_count"] == 1

    courses = grading_service.grading_store.list_courses()
    assert [course.course_code for course in courses] == ["DEMO101"]
    tests = grading_service.grading_store.list_tests(courses[0].id)
    assert [test.test_name for test in tests] == ["Demo Test 1", "Demo Test 2"]
    assert all(test.max_attempts == 10 for test in tests)
    assert all(
        [question.external_id for question in test.questions] == ["1.1", "1.2", "1.6"]
        and all(question.grading_method == "rubric" for question in test.questions)
        for test in tests
    )
    assert [
        attempt.status
        for test in tests
        for student in identity_service.repository.list()
        if student.role == "student"
        for attempt in grading_service.grading_store.list_attempts(test.id, student.id)
    ] == ["graded"] * 12


def test_startup_preserves_existing_account_credentials_and_inactive_status(
    settings, identity_service
):
    existing = asyncio.run(
        identity_service.register_student(
            StudentRegistration(
                email="student@example.com",
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
    assert len(identity_service.repository.list()) == 3
    _, password_hash = original
    assert identity_service.password_hasher.verify(
        "existing-student-password", password_hash
    )
    assert not identity_service.password_hasher.verify("Password123!", password_hash)


def test_startup_seeding_can_be_disabled(client, repository):
    assert repository.list() == []
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "instructor@example.com", "password": "Password123!"},
    )
    assert response.status_code == 401
