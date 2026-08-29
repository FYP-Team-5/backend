from __future__ import annotations

from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

from app.dto import RetrievedRubricChunk


class QdrantStoreError(RuntimeError):
    """Raised when shared rubric chunks cannot be read."""


class QdrantPayloadError(QdrantStoreError):
    """Raised when Qdrant data does not match PostgreSQL metadata."""


class QdrantRubricChunkRepository:
    def __init__(
        self,
        *,
        url: str,
        api_key: str | None,
        collection: str,
        timeout: float = 30,
        client: QdrantClient | None = None,
    ) -> None:
        self.collection = collection
        self._client = client or QdrantClient(
            url=url,
            api_key=api_key,
            timeout=timeout,
            check_compatibility=False,
        )

    def close(self) -> None:
        self._client.close()

    def health(self) -> bool:
        try:
            self._client.get_collection(self.collection)
            return True
        except (ResponseHandlingException, UnexpectedResponse, OSError):
            return False

    def retrieve(
        self,
        *,
        chunk_ids: list[str],
        rubric_id: str,
        document_id: str,
    ) -> list[RetrievedRubricChunk]:
        try:
            records = self._client.retrieve(
                collection_name=self.collection,
                ids=chunk_ids,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:
            raise QdrantStoreError("Unable to retrieve rubric chunks from Qdrant.") from exc

        records_by_id = {str(record.id): record for record in records}
        missing = [chunk_id for chunk_id in chunk_ids if chunk_id not in records_by_id]
        if missing:
            raise QdrantPayloadError(
                f"Qdrant is missing {len(missing)} chunk(s) referenced by PostgreSQL."
            )

        chunks: list[RetrievedRubricChunk] = []
        for chunk_id in chunk_ids:
            payload: Any = records_by_id[chunk_id].payload or {}
            if not isinstance(payload, dict):
                raise QdrantPayloadError(f"Chunk '{chunk_id}' has an invalid payload.")
            chunk_metadata = payload.get("metadata")
            if not isinstance(chunk_metadata, dict):
                raise QdrantPayloadError(f"Chunk '{chunk_id}' has invalid metadata.")
            if chunk_metadata.get("rubric_id") != rubric_id:
                raise QdrantPayloadError(f"Chunk '{chunk_id}' belongs to another rubric.")
            if chunk_metadata.get("document_id") != document_id:
                raise QdrantPayloadError(f"Chunk '{chunk_id}' belongs to another document.")
            content = payload.get("page_content")
            if not isinstance(content, str) or not content.strip():
                raise QdrantPayloadError(f"Chunk '{chunk_id}' has no text content.")
            chunks.append(
                RetrievedRubricChunk(
                    id=chunk_id,
                    content=content,
                    metadata={
                        key: value
                        for key, value in chunk_metadata.items()
                        if not key.startswith("_")
                    },
                )
            )
        return chunks
