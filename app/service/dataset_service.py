"""Durable CSV publication with independent storage health and automatic collection."""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
from contextlib import suppress

from app.db.dataset_repository import DatasetRepository
from app.db.grading_repository import GradingConflictError
from app.db.grading_workflow import LeaseLostError
from app.service.dataset_storage import DatasetStorage, DatasetStorageError
from app.service.training_csv import parse_training_csv

logger = logging.getLogger(__name__)


class DatasetService:
    def __init__(self, settings, grading_store, *, storage=None):
        self.settings = settings
        self.repository = DatasetRepository(grading_store)
        self.storage = storage or DatasetStorage(settings)
        self.storage_status = "unknown"
        self._task = None
        self._wake = asyncio.Event()

    async def initialize(self):
        await asyncio.to_thread(self.repository.initialize)
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(self._worker(), name="training-dataset-worker")

    async def close(self):
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        await asyncio.to_thread(self.storage.close)

    async def storage_health(self):
        try:
            await asyncio.to_thread(self.storage.ensure_bucket)
            self.storage_status = "ok"
        except DatasetStorageError:
            self.storage_status = "unavailable"
        return {"status": self.storage_status}

    async def upload(self, source, creator_id, filename, request_key=None):
        # The controller has already streamed and bounded the multipart file.
        source.seek(0)
        checksum = hashlib.sha256()
        while chunk := source.read(128 * 1024):
            checksum.update(chunk)
        source.seek(0)
        rows = await asyncio.to_thread(parse_training_csv, source)
        result = await asyncio.to_thread(
            self.repository.reserve_upload,
            rows,
            checksum.hexdigest(),
            creator_id,
            filename,
            request_key or checksum.hexdigest(),
        )
        self._wake.set()
        return result

    async def list_versions(self):
        return await asyncio.to_thread(self.repository.list_versions)

    async def get_version(self, version_id):
        _, metadata = await asyncio.to_thread(self.repository.get_version, version_id)
        return metadata

    async def download(self, version_id=None):
        row, metadata = await asyncio.to_thread(self.repository.get_version, version_id)
        if row["status"] != "ready":
            raise GradingConflictError("This CSV version is not ready for download.")
        try:
            file = await asyncio.to_thread(
                self.storage.download_csv,
                row["object_key"],
                row["checksum"],
                row["byte_size"],
            )
            self.storage_status = "ok"
            return file, metadata
        except DatasetStorageError:
            self.storage_status = "unavailable"
            raise

    async def retry(self, version_id, creator_id):
        result = await asyncio.to_thread(self.repository.retry, version_id, creator_id)
        self._wake.set()
        return result

    async def export_status(self, test_id):
        return await asyncio.to_thread(self.repository.export_status, test_id)

    async def retry_export(self, test_id, creator_id):
        await asyncio.to_thread(self.repository.reconcile)
        status = await self.export_status(test_id)
        if status["version_id"]:
            version = await self.get_version(status["version_id"])
            if version["status"] == "failed":
                await self.retry(version["id"], creator_id)
        self._wake.set()
        return status

    async def _worker(self):
        while True:
            self._wake.clear()
            try:
                if await self.process_pending():
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Dataset worker could not reconcile work")
            try:
                await asyncio.wait_for(
                    self._wake.wait(), self.settings.dataset_poll_seconds
                )
            except TimeoutError:
                pass

    async def process_pending(self):
        await asyncio.to_thread(self.repository.reconcile)
        job = await asyncio.to_thread(
            self.repository.claim,
            self.settings.dataset_lease_seconds,
            self.settings.dataset_max_retries,
        )
        if job is None:
            return False
        heartbeat = asyncio.create_task(self._heartbeat(job))
        try:
            if job["exhausted"]:
                await asyncio.to_thread(
                    self.repository.fail,
                    job,
                    "Dataset publication retry limit reached.",
                )
                return True
            prepared = await asyncio.to_thread(self.repository.prepare, job)
            if prepared is None:
                return True
            await asyncio.to_thread(self.storage.ensure_bucket)
            await asyncio.to_thread(
                self.storage.put_csv,
                prepared["object_key"],
                io.BytesIO(prepared["payload"]),
                prepared["checksum"],
                prepared["byte_size"],
            )
            self.storage_status = "ok"
            published = await asyncio.to_thread(self.repository.activate, job, prepared)
            if published:
                for key in prepared["staged_keys"]:
                    if key != prepared["object_key"]:
                        with suppress(DatasetStorageError):
                            await asyncio.to_thread(self.storage.delete_staged, key)
        except asyncio.CancelledError:
            await asyncio.to_thread(self.repository.release, job)
            raise
        except LeaseLostError:
            logger.info("Dataset worker lost lease %s", job["id"])
        except Exception as exc:
            logger.exception("Dataset publication failed")
            if isinstance(exc, DatasetStorageError):
                self.storage_status = "unavailable"
            with suppress(LeaseLostError):
                await asyncio.to_thread(
                    self.repository.fail,
                    job,
                    str(exc)
                    if isinstance(exc, DatasetStorageError)
                    else "Dataset publication failed; retry or check server logs.",
                    retry=job["claim_count"] <= self.settings.dataset_max_retries,
                    delay=self.settings.dataset_retry_seconds
                    * 2 ** (job["claim_count"] - 1),
                )
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        return True

    async def _heartbeat(self, job):
        while True:
            await asyncio.sleep(self.settings.dataset_lease_seconds / 3)
            try:
                if not await asyncio.to_thread(
                    self.repository.renew, job, self.settings.dataset_lease_seconds
                ):
                    return
            except Exception:
                logger.exception("Could not renew dataset lease")
                return
