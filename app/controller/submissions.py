"""Instructor review, test availability, and student grading retries."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path

from app.controller.dependencies import (
    ID_PATTERN,
    current_user_id,
    get_attempt_service,
    get_catalog_service,
    get_user_service,
    require_api_key,
    require_instructor,
)
from app.db import AttemptStateError, GradingConflictError, GradingRecordNotFoundError
from app.dto import (
    AttemptGradeResponse,
    StudentSubmissionSummary,
    SubmissionGradeUpdate,
    TestDeadlineUpdate,
    UserResponse,
)
from app.model import Attempt, Test
from app.service import AttemptService, CatalogService, UserService

router = APIRouter(
    prefix="/tests", tags=["submissions"], dependencies=[Depends(require_api_key)]
)
TestId = Annotated[str, Path(pattern=ID_PATTERN.pattern)]
AttemptId = Annotated[str, Path(pattern=ID_PATTERN.pattern)]
Instructor = Annotated[UserResponse, Depends(require_instructor)]
Attempts = Annotated[AttemptService, Depends(get_attempt_service)]
Catalog = Annotated[CatalogService, Depends(get_catalog_service)]


def workflow_error(exc: Exception) -> HTTPException:
    if isinstance(exc, GradingRecordNotFoundError):
        return HTTPException(status_code=404, detail="Test or submission not found.")
    return HTTPException(status_code=409, detail=str(exc))


@router.get("/{test_id}/submissions", response_model=list[StudentSubmissionSummary])
async def list_submissions(
    test_id: TestId,
    principal: Instructor,
    service: Attempts,
    users: Annotated[UserService, Depends(get_user_service)],
):
    try:
        students = await users.list_users(principal, role="student")
        return await service.list_submissions(test_id, students)
    except GradingRecordNotFoundError as exc:
        raise workflow_error(exc) from exc


@router.get("/{test_id}/submissions/{attempt_id}", response_model=AttemptGradeResponse)
async def get_submission(
    test_id: TestId, attempt_id: AttemptId, principal: Instructor, service: Attempts
):
    try:
        return await service.get_submission(test_id, attempt_id)
    except GradingRecordNotFoundError as exc:
        raise workflow_error(exc) from exc


@router.patch(
    "/{test_id}/submissions/{attempt_id}/grades", response_model=AttemptGradeResponse
)
async def edit_submission(
    test_id: TestId,
    attempt_id: AttemptId,
    body: SubmissionGradeUpdate,
    principal: Instructor,
    service: Attempts,
):
    try:
        return await service.edit_submission(test_id, attempt_id, principal.id, body)
    except (GradingRecordNotFoundError, GradingConflictError, AttemptStateError) as exc:
        raise workflow_error(exc) from exc


@router.patch("/{test_id}/deadline", response_model=Test)
async def set_deadline(
    test_id: TestId, body: TestDeadlineUpdate, principal: Instructor, service: Catalog
):
    try:
        return await service.set_deadline(test_id, body.ends_at)
    except (GradingRecordNotFoundError, GradingConflictError) as exc:
        raise workflow_error(exc) from exc


@router.post("/{test_id}/end", response_model=Test)
async def end_test(test_id: TestId, principal: Instructor, service: Catalog):
    try:
        return await service.end_test(test_id, principal.id)
    except GradingRecordNotFoundError as exc:
        raise workflow_error(exc) from exc


@router.post("/{test_id}/attempts/{attempt_id}/retry-grading", response_model=Attempt)
async def retry_grading(
    test_id: TestId,
    attempt_id: AttemptId,
    user_id: Annotated[str, Depends(current_user_id)],
    service: Attempts,
):
    try:
        return await service.retry_grading(test_id, attempt_id, user_id)
    except (GradingRecordNotFoundError, AttemptStateError) as exc:
        raise workflow_error(exc) from exc
