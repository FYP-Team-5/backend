"""Instructor-only CSV dataset upload, immutable downloads, and export status."""

from pathlib import PurePath
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Path,
    Request,
    UploadFile,
)
from fastapi.responses import StreamingResponse

from app.controller.dependencies import ID_PATTERN, require_api_key, require_instructor
from app.db import GradingConflictError, GradingRecordNotFoundError
from app.dto import UserResponse
from app.service.dataset_service import DatasetService
from app.service.dataset_storage import DatasetStorageError
from app.service.training_csv import TrainingCsvError

router = APIRouter(
    tags=["training dataset"],
    dependencies=[Depends(require_api_key), Depends(require_instructor)],
)


def get_dataset_service(request: Request) -> DatasetService:
    return request.app.state.dataset_service


Service = Annotated[DatasetService, Depends(get_dataset_service)]
Instructor = Annotated[UserResponse, Depends(require_instructor)]
RecordId = Annotated[str, Path(pattern=ID_PATTERN.pattern)]


def translate_error(exc):
    if isinstance(exc, GradingRecordNotFoundError):
        return HTTPException(
            status_code=404, detail="Dataset version or test not found."
        )
    if isinstance(exc, DatasetStorageError):
        return HTTPException(status_code=503, detail=str(exc))
    return HTTPException(status_code=409, detail=str(exc))


@router.get("/training-dataset/status")
async def storage_status(service: Service):
    return await service.storage_health()


@router.post("/training-dataset/versions", status_code=202)
async def upload_dataset(
    service: Service,
    principal: Instructor,
    request: Request,
    file: Annotated[UploadFile, File()],
    idempotency_key: Annotated[
        str | None, Header(max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    ] = None,
):
    filename = PurePath(file.filename or "dataset.csv").name
    if not filename.lower().endswith(".csv") or len(filename) > 255:
        raise HTTPException(
            status_code=422,
            detail="Upload a .csv file with a filename of at most 255 characters.",
        )
    maximum = request.app.state.settings.dataset_upload_max_bytes
    try:
        size = 0
        while chunk := await file.read(128 * 1024):
            size += len(chunk)
            if size > maximum:
                raise HTTPException(
                    status_code=413,
                    detail=f"CSV exceeds the {maximum}-byte upload limit.",
                )
        await file.seek(0)
        return await service.upload(file.file, principal.id, filename, idempotency_key)
    except TrainingCsvError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except GradingConflictError as exc:
        raise translate_error(exc) from exc
    finally:
        await file.close()


@router.get("/training-dataset/versions")
async def list_versions(service: Service):
    return await service.list_versions()


@router.get("/training-dataset/versions/{version_id}")
async def get_version(version_id: RecordId, service: Service):
    try:
        return await service.get_version(version_id)
    except GradingRecordNotFoundError as exc:
        raise translate_error(exc) from exc


async def csv_download(service, version_id=None):
    try:
        source, metadata = await service.download(version_id)
    except (
        GradingRecordNotFoundError,
        GradingConflictError,
        DatasetStorageError,
    ) as exc:
        raise translate_error(exc) from exc

    def chunks():
        try:
            while chunk := source.read(128 * 1024):
                yield chunk
        finally:
            source.close()

    return StreamingResponse(
        chunks(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="training-dataset-v{metadata["number"]:06d}.csv"',
            "Content-Length": str(metadata["byte_size"]),
            "ETag": f'"{metadata["checksum"]}"',
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/training-dataset/download")
async def download_latest(service: Service):
    return await csv_download(service)


@router.get("/training-dataset/versions/{version_id}/download")
async def download_version(version_id: RecordId, service: Service):
    return await csv_download(service, version_id)


@router.post("/training-dataset/versions/{version_id}/retry", status_code=202)
async def retry_version(version_id: RecordId, service: Service, principal: Instructor):
    try:
        return await service.retry(version_id, principal.id)
    except (GradingRecordNotFoundError, GradingConflictError) as exc:
        raise translate_error(exc) from exc


@router.get("/tests/{test_id}/training-export")
async def export_status(test_id: RecordId, service: Service):
    try:
        return await service.export_status(test_id)
    except GradingRecordNotFoundError as exc:
        raise translate_error(exc) from exc


@router.post("/tests/{test_id}/training-export/retry", status_code=202)
async def retry_export(test_id: RecordId, service: Service, principal: Instructor):
    try:
        return await service.retry_export(test_id, principal.id)
    except GradingRecordNotFoundError as exc:
        raise translate_error(exc) from exc
