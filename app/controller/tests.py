from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path

from app.controller.dependencies import (
    ID_PATTERN,
    get_attempt_service,
    get_catalog_service,
    require_api_key,
    require_user_id,
)
from app.db import (
    AttemptStateError,
    GradingConflictError,
    GradingRecordNotFoundError,
    GradingStoreError,
)
from app.dto import (
    AttemptGradeResponse,
    GradeAttemptRequest,
    RubricCreate,
)
from app.model import Attempt, Question, Test
from app.service import (
    AttemptService,
    CatalogService,
    IncompleteAttemptError,
    LLMCriteriaMismatchError,
    LLMResponseError,
    LLMScoreScaleError,
    LLMServiceError,
    RubricNotAssignedError,
    StudentAnswerTooLargeError,
    UnknownQuestionError,
)

tests_router = APIRouter(prefix="/tests", tags=["catalog"], dependencies=[Depends(require_api_key)])

@tests_router.get("/{test_id}", response_model=Test)
async def get_test(
    test_id: Annotated[str, Path(pattern=ID_PATTERN.pattern)],
    service: Annotated[CatalogService, Depends(get_catalog_service)],
) -> Test:
    try:
        return await service.get_test(test_id)
    except GradingRecordNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Test not found.") from exc


@tests_router.put(
    "/{test_id}/questions/{question_id}/rubric",
    response_model=Question,
)
async def set_question_rubric(
    test_id: Annotated[str, Path(pattern=ID_PATTERN.pattern)],
    question_id: Annotated[str, Path(pattern=ID_PATTERN.pattern)],
    body: RubricCreate,
    service: Annotated[CatalogService, Depends(get_catalog_service)],
) -> Question:
    try:
        return await service.set_question_rubric(test_id, question_id, body)
    except GradingRecordNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Test or question not found.") from exc
    except GradingConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except GradingStoreError as exc:
        raise HTTPException(status_code=502, detail="Grading database failed.") from exc


@tests_router.post(
    "/{test_id}/attempts",
    response_model=Attempt,
    status_code=201,
    tags=["attempts"],
)
async def create_attempt(
    test_id: Annotated[str, Path(pattern=ID_PATTERN.pattern)],
    user_id: Annotated[str, Depends(require_user_id)],
    service: Annotated[AttemptService, Depends(get_attempt_service)],
) -> Attempt:
    try:
        return await service.create_attempt(test_id, user_id)
    except GradingRecordNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Test not found.") from exc
    except RubricNotAssignedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except GradingConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except GradingStoreError as exc:
        raise HTTPException(status_code=502, detail="Grading database failed.") from exc


@tests_router.get(
    "/{test_id}/attempts",
    response_model=list[Attempt],
    tags=["attempts"],
)
async def list_attempts(
    test_id: Annotated[str, Path(pattern=ID_PATTERN.pattern)],
    user_id: Annotated[str, Depends(require_user_id)],
    service: Annotated[AttemptService, Depends(get_attempt_service)],
) -> list[Attempt]:
    try:
        return await service.list_attempts(test_id, user_id)
    except GradingRecordNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Test not found.") from exc


@tests_router.post(
    "/{test_id}/attempts/{attempt_id}/grade",
    response_model=AttemptGradeResponse,
    tags=["grading"],
)
async def grade_attempt(
    test_id: Annotated[str, Path(pattern=ID_PATTERN.pattern)],
    attempt_id: str,
    body: GradeAttemptRequest,
    user_id: Annotated[str, Depends(require_user_id)],
    service: Annotated[AttemptService, Depends(get_attempt_service)],
) -> AttemptGradeResponse:
    try:
        return await service.grade_attempt(test_id, attempt_id, user_id, body)
    except GradingRecordNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail="Test or attempt not found."
        ) from exc
    except AttemptStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except StudentAnswerTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except UnknownQuestionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (LLMResponseError, LLMScoreScaleError, LLMCriteriaMismatchError) as exc:
        raise HTTPException(
            status_code=502, detail="LLM returned an invalid grade."
        ) from exc
    except IncompleteAttemptError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LLMServiceError as exc:
        raise HTTPException(
            status_code=502, detail="LLM grading request failed."
        ) from exc
    except GradingStoreError as exc:
        raise HTTPException(status_code=502, detail="Grading database failed.") from exc


@tests_router.get(
    "/{test_id}/attempts/{attempt_id}",
    response_model=AttemptGradeResponse,
    tags=["attempts"],
)
async def get_attempt_result(
    test_id: Annotated[str, Path(pattern=ID_PATTERN.pattern)],
    attempt_id: str,
    user_id: Annotated[str, Depends(require_user_id)],
    service: Annotated[AttemptService, Depends(get_attempt_service)],
) -> AttemptGradeResponse:
    try:
        return await service.get_attempt_result(test_id, attempt_id, user_id)
    except GradingRecordNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail="Test or attempt not found."
        ) from exc
    except AttemptStateError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
