from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Path, UploadFile
from pydantic import AwareDatetime, ValidationError

from app.controller.dependencies import (
    ID_PATTERN,
    current_user,
    get_catalog_service,
    require_api_key,
    require_instructor,
)
from app.controller.presenters import test_view
from app.db import (
    GradingConflictError,
    GradingRecordNotFoundError,
    GradingStoreError,
)
from app.dto import (
    CourseCreate,
    TestCreate,
    UserResponse,
)
from app.model import Course, Test
from app.service import (
    CatalogService,
    CsvFormatError,
)

courses_router = APIRouter(prefix="/courses", tags=["catalog"], dependencies=[Depends(require_api_key)])

@courses_router.post("", response_model=Course, status_code=201, dependencies=[Depends(require_instructor)])
async def create_course(
    body: CourseCreate,
    service: Annotated[CatalogService, Depends(get_catalog_service)],
) -> Course:
    try:
        return await service.create_course(body)
    except GradingConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except GradingStoreError as exc:
        raise HTTPException(status_code=502, detail="Grading database failed.") from exc


@courses_router.get("", response_model=list[Course])
async def list_courses(
    service: Annotated[CatalogService, Depends(get_catalog_service)],
) -> list[Course]:
    return await service.list_courses()


@courses_router.post(
    "/{course_id}/tests",
    response_model=Test,
    status_code=201,
    dependencies=[Depends(require_instructor)],
)
async def create_test(
    course_id: Annotated[str, Path(pattern=ID_PATTERN.pattern)],
    body: TestCreate,
    service: Annotated[CatalogService, Depends(get_catalog_service)],
) -> Test:
    try:
        return await service.create_test(course_id, body)
    except GradingRecordNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Course or rubric not found.") from exc
    except GradingConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except GradingStoreError as exc:
        raise HTTPException(status_code=502, detail="Grading database failed.") from exc


@courses_router.post(
    "/{course_id}/tests/csv",
    response_model=Test,
    status_code=201,
    dependencies=[Depends(require_instructor)],
)
async def create_test_from_csv(
    course_id: Annotated[str, Path(pattern=ID_PATTERN.pattern)],
    service: Annotated[CatalogService, Depends(get_catalog_service)],
    file: Annotated[UploadFile, File()],
    test_name: Annotated[str, Form(min_length=1, max_length=300)],
    ends_at: Annotated[AwareDatetime, Form()],
    max_attempts: Annotated[int, Form(ge=1, le=100)] = 1,
) -> Test:
    try:
        content = (await file.read()).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="CSV file must be UTF-8 encoded.") from exc
    try:
        return await service.create_test_from_csv(course_id, test_name, max_attempts, content, ends_at)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except CsvFormatError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except GradingRecordNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Course not found.") from exc
    except GradingConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except GradingStoreError as exc:
        raise HTTPException(status_code=502, detail="Grading database failed.") from exc


@courses_router.get(
    "/{course_id}/tests",
    response_model=list[Test],
)
async def list_tests(
    principal: Annotated[UserResponse, Depends(current_user)],
    course_id: Annotated[str, Path(pattern=ID_PATTERN.pattern)],
    service: Annotated[CatalogService, Depends(get_catalog_service)],
) -> list[Test]:
    try:
        return [test_view(test, principal.role) for test in await service.list_tests(course_id)]
    except GradingRecordNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Course not found.") from exc
