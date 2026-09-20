from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Path, UploadFile

from app.controller.dependencies import (
    ID_PATTERN,
    current_user_id,
    get_attempt_service,
    get_catalog_service,
    require_api_key,
)
from app.db import (
    AttemptStateError,
    GradingConflictError,
    GradingRecordNotFoundError,
    GradingStoreError,
)
from app.dto import (
    AttemptGradeResponse,
    FewShotGradeRequest,
    FewShotGradingResult,
    GradeAttemptRequest,
    GradingMethodUpdate,
    RubricCreate,
)
from app.model import Attempt, Question, Test
from app.service import (
    AttemptService,
    CatalogService,
    CsvFormatError,
    ExamplesNotAssignedError,
    GradingMethodAmbiguousError,
    GradingMethodNotAssignedError,
    LLMResponseError,
    LLMScoreScaleError,
    LLMServiceError,
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


@tests_router.put(
    "/{test_id}/questions/{question_id}/grading-method",
    response_model=Question,
)
async def set_grading_method(
    test_id: Annotated[str, Path(pattern=ID_PATTERN.pattern)],
    question_id: Annotated[str, Path(pattern=ID_PATTERN.pattern)],
    body: GradingMethodUpdate,
    service: Annotated[CatalogService, Depends(get_catalog_service)],
) -> Question:
    """Explicitly picks which method grades this question. Only required
    when a question has both a rubric and few-shot examples attached —
    otherwise whichever one is present is used automatically.
    """
    try:
        return await service.set_grading_method(test_id, question_id, body.method)
    except GradingRecordNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Test or question not found.") from exc
    except GradingStoreError as exc:
        raise HTTPException(status_code=502, detail="Grading database failed.") from exc


@tests_router.post(
    "/{test_id}/criteria/csv",
    response_model=Test,
)
async def upload_criteria_csv(
    test_id: Annotated[str, Path(pattern=ID_PATTERN.pattern)],
    service: Annotated[CatalogService, Depends(get_catalog_service)],
    file: Annotated[UploadFile, File()],
) -> Test:
    try:
        content = (await file.read()).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="CSV file must be UTF-8 encoded.") from exc
    try:
        return await service.upload_criteria_csv(test_id, content)
    except CsvFormatError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except UnknownQuestionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except GradingRecordNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Test or question not found.") from exc
    except GradingConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except GradingStoreError as exc:
        raise HTTPException(status_code=502, detail="Grading database failed.") from exc


@tests_router.post(
    "/{test_id}/examples/csv",
    response_model=Test,
)
async def upload_examples_csv(
    test_id: Annotated[str, Path(pattern=ID_PATTERN.pattern)],
    service: Annotated[CatalogService, Depends(get_catalog_service)],
    file: Annotated[UploadFile, File()],
) -> Test:
    try:
        content = (await file.read()).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="CSV file must be UTF-8 encoded.") from exc
    try:
        return await service.upload_examples_csv(test_id, content)
    except CsvFormatError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except UnknownQuestionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except GradingRecordNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Test or question not found.") from exc
    except GradingConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except GradingStoreError as exc:
        raise HTTPException(status_code=502, detail="Grading database failed.") from exc


@tests_router.post(
    "/{test_id}/questions/{question_id}/grade-fewshot",
    response_model=FewShotGradingResult,
    tags=["grading"],
)
async def grade_fewshot(
    test_id: Annotated[str, Path(pattern=ID_PATTERN.pattern)],
    question_id: Annotated[str, Path(pattern=ID_PATTERN.pattern)],
    body: FewShotGradeRequest,
    service: Annotated[CatalogService, Depends(get_catalog_service)],
) -> FewShotGradingResult:
    """Grades one answer via few-shot prompting for comparison against the
    rubric-based attempt flow. Not persisted and not part of the student
    attempt lifecycle — intended for method comparison only.
    """
    try:
        return await service.grade_fewshot(test_id, question_id, body.answer)
    except GradingRecordNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Test not found.") from exc
    except UnknownQuestionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ExamplesNotAssignedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except StudentAnswerTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except LLMScoreScaleError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (LLMServiceError, LLMResponseError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@tests_router.post(
    "/{test_id}/attempts",
    response_model=Attempt,
    status_code=201,
    tags=["attempts"],
)
async def create_attempt(
    test_id: Annotated[str, Path(pattern=ID_PATTERN.pattern)],
    user_id: Annotated[str, Depends(current_user_id)],
    service: Annotated[AttemptService, Depends(get_attempt_service)],
) -> Attempt:
    try:
        return await service.create_attempt(test_id, user_id)
    except GradingRecordNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Test not found.") from exc
    except (GradingMethodNotAssignedError, GradingMethodAmbiguousError) as exc:
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
    user_id: Annotated[str, Depends(current_user_id)],
    service: Annotated[AttemptService, Depends(get_attempt_service)],
) -> list[Attempt]:
    try:
        return await service.list_attempts(test_id, user_id)
    except GradingRecordNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Test not found.") from exc


@tests_router.post(
    "/{test_id}/attempts/{attempt_id}/grade",
    response_model=Attempt,
    tags=["grading"],
)
async def grade_attempt(
    test_id: Annotated[str, Path(pattern=ID_PATTERN.pattern)],
    attempt_id: str,
    body: GradeAttemptRequest,
    user_id: Annotated[str, Depends(current_user_id)],
    service: Annotated[AttemptService, Depends(get_attempt_service)],
) -> Attempt:
    """Saves the submitted answers and starts grading in the background.
    Poll GET .../attempts/{attempt_id} for the graded result — LLM grading
    happens per-question and isn't done by the time this returns.
    """
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
    user_id: Annotated[str, Depends(current_user_id)],
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
