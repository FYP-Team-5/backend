from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path

from app.controller.dependencies import get_catalog_service, ID_PATTERN, require_api_key
from app.db import (
    GradingConflictError,
    GradingRecordNotFoundError,
    GradingStoreError,
)
from app.dto import (
    CourseCreate,
    ExamCreate,
)
from app.model import Course, Exam
from app.service import (
    CatalogService,
)

courses_router = APIRouter(prefix="/courses", tags=["catalog"], dependencies=[Depends(require_api_key)])

@courses_router.post("", response_model=Course, status_code=201)
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
    "/{course_id}/exams",
    response_model=Exam,
    status_code=201,
)
async def create_exam(
    course_id: Annotated[str, Path(pattern=ID_PATTERN.pattern)],
    body: ExamCreate,
    service: Annotated[CatalogService, Depends(get_catalog_service)],
) -> Exam:
    try:
        return await service.create_exam(course_id, body)
    except GradingRecordNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Course not found.") from exc
    except GradingConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except GradingStoreError as exc:
        raise HTTPException(status_code=502, detail="Grading database failed.") from exc


@courses_router.get(
    "/{course_id}/exams",
    response_model=list[Exam],
)
async def list_exams(
    course_id: Annotated[str, Path(pattern=ID_PATTERN.pattern)],
    service: Annotated[CatalogService, Depends(get_catalog_service)],
) -> list[Exam]:
    try:
        return await service.list_exams(course_id)
    except GradingRecordNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Course not found.") from exc
