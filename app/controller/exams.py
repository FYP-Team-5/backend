from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path

from app.controller.dependencies import require_api_key, ID_PATTERN, get_catalog_service, require_student_id, get_attempt_service
from app.db import (
    AttemptLimitExceededError,
    AttemptStateError,
    GradingConflictError,
    GradingRecordNotFoundError,
    GradingStoreError,
    MetadataStoreError,
    QdrantStoreError,
    RubricMetadataNotFoundError,
)
from app.dto import (
    AttemptGradeResponse,
    ExamRubricUpdate,
    GradeAttemptRequest,
    RubricChunkMappingRequest,
)
from app.model import Attempt, Course, Exam, Question
from app.service import (
    AttemptService,
    CatalogService,
    IncompleteAttemptError,
    LLMResponseError,
    LLMScoreScaleError,
    LLMServiceError,
    RubricChunkMappingError,
    RubricChunksMissingError,
    RubricOwnershipError,
    RubricProcessingIncompleteError,
    StudentAnswerTooLargeError,
)

exams_router = APIRouter(prefix="/exams", tags=["catalog"], dependencies=[Depends(require_api_key)])

@exams_router.get("/{exam_id}", response_model=Exam)
async def get_exam(
    exam_id: Annotated[str, Path(pattern=ID_PATTERN.pattern)],
    service: Annotated[CatalogService, Depends(get_catalog_service)],
) -> Exam:
    try:
        return await service.get_exam(exam_id)
    except GradingRecordNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Exam not found.") from exc


@exams_router.put("/{exam_id}/rubric", response_model=Exam)
async def update_exam_rubric(
    exam_id: Annotated[str, Path(pattern=ID_PATTERN.pattern)],
    body: ExamRubricUpdate,
    service: Annotated[CatalogService, Depends(get_catalog_service)],
) -> Exam:
    try:
        return await service.update_exam_rubric(exam_id, body)
    except (GradingRecordNotFoundError, RubricMetadataNotFoundError) as exc:
        raise HTTPException(
            status_code=404, detail="Exam or rubric not found."
        ) from exc
    except (RubricOwnershipError, RubricProcessingIncompleteError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except GradingConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MetadataStoreError as exc:
        raise HTTPException(status_code=502, detail="Rubric metadata failed.") from exc
    except GradingStoreError as exc:
        raise HTTPException(status_code=502, detail="Grading database failed.") from exc


@exams_router.put(
    "/{exam_id}/questions/{question_id}/rubric-chunks",
    response_model=Question,
)
async def map_question_chunks(
    exam_id: Annotated[str, Path(pattern=ID_PATTERN.pattern)],
    question_id: Annotated[str, Path(pattern=ID_PATTERN.pattern)],
    body: RubricChunkMappingRequest,
    service: Annotated[CatalogService, Depends(get_catalog_service)],
) -> Question:
    try:
        return await service.map_question_chunks(
            exam_id, question_id, body.chunk_indexes
        )
    except (GradingRecordNotFoundError, RubricMetadataNotFoundError) as exc:
        raise HTTPException(
            status_code=404, detail="Exam, question, or rubric not found."
        ) from exc
    except (RubricChunkMappingError, RubricOwnershipError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RubricProcessingIncompleteError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (MetadataStoreError, RubricChunksMissingError, QdrantStoreError) as exc:
        raise HTTPException(status_code=502, detail="Rubric storage failed.") from exc


@exams_router.post(
    "/{exam_id}/attempts",
    response_model=Attempt,
    status_code=201,
    tags=["attempts"],
)
async def create_attempt(
    exam_id: Annotated[str, Path(pattern=ID_PATTERN.pattern)],
    student_id: Annotated[str, Depends(require_student_id)],
    service: Annotated[AttemptService, Depends(get_attempt_service)],
) -> Attempt:
    try:
        return await service.create_attempt(exam_id, student_id)
    except GradingRecordNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Exam not found.") from exc
    except RubricMetadataNotFoundError as exc:
        raise HTTPException(
            status_code=409, detail="Exam rubric has not been uploaded."
        ) from exc
    except AttemptLimitExceededError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (
        RubricChunkMappingError,
        RubricOwnershipError,
        RubricProcessingIncompleteError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except GradingConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (MetadataStoreError, RubricChunksMissingError, QdrantStoreError) as exc:
        raise HTTPException(status_code=502, detail="Rubric storage failed.") from exc
    except GradingStoreError as exc:
        raise HTTPException(status_code=502, detail="Grading database failed.") from exc


@exams_router.get(
    "/{exam_id}/attempts",
    response_model=list[Attempt],
    tags=["attempts"],
)
async def list_attempts(
    exam_id: Annotated[str, Path(pattern=ID_PATTERN.pattern)],
    student_id: Annotated[str, Depends(require_student_id)],
    service: Annotated[AttemptService, Depends(get_attempt_service)],
) -> list[Attempt]:
    try:
        return await service.list_attempts(exam_id, student_id)
    except GradingRecordNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Exam not found.") from exc


@exams_router.post(
    "/{exam_id}/attempts/{attempt_id}/grade",
    response_model=AttemptGradeResponse,
    tags=["grading"],
)
async def grade_attempt(
    exam_id: Annotated[str, Path(pattern=ID_PATTERN.pattern)],
    attempt_id: str,
    body: GradeAttemptRequest,
    student_id: Annotated[str, Depends(require_student_id)],
    service: Annotated[AttemptService, Depends(get_attempt_service)],
) -> AttemptGradeResponse:
    try:
        return await service.grade_attempt(exam_id, attempt_id, student_id, body)
    except GradingRecordNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail="Exam or attempt not found."
        ) from exc
    except RubricMetadataNotFoundError as exc:
        raise HTTPException(
            status_code=409, detail="Attempt rubric is unavailable."
        ) from exc
    except RubricProcessingIncompleteError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (AttemptStateError, IncompleteAttemptError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (RubricOwnershipError, RubricChunkMappingError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except StudentAnswerTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except (MetadataStoreError, RubricChunksMissingError, QdrantStoreError) as exc:
        raise HTTPException(status_code=502, detail="Rubric storage failed.") from exc
    except (LLMResponseError, LLMScoreScaleError) as exc:
        raise HTTPException(
            status_code=502, detail="LLM returned an invalid grade."
        ) from exc
    except LLMServiceError as exc:
        raise HTTPException(
            status_code=502, detail="LLM grading request failed."
        ) from exc
    except GradingStoreError as exc:
        raise HTTPException(status_code=502, detail="Grading database failed.") from exc


@exams_router.get(
    "/{exam_id}/attempts/{attempt_id}",
    response_model=AttemptGradeResponse,
    tags=["attempts"],
)
async def get_attempt_result(
    exam_id: Annotated[str, Path(pattern=ID_PATTERN.pattern)],
    attempt_id: str,
    student_id: Annotated[str, Depends(require_student_id)],
    service: Annotated[AttemptService, Depends(get_attempt_service)],
) -> AttemptGradeResponse:
    try:
        return await service.get_attempt_result(exam_id, attempt_id, student_id)
    except GradingRecordNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail="Exam or attempt not found."
        ) from exc
    except AttemptStateError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
