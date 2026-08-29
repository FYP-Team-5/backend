from types import SimpleNamespace

import pytest

from app.db import QdrantPayloadError, QdrantRubricChunkRepository


class FakeQdrantClient:
    def __init__(self, records=None) -> None:
        self.records = records or []
        self.retrieve_call = None
        self.closed = False

    def get_collection(self, collection: str):
        return SimpleNamespace(status="green")

    def retrieve(self, **kwargs):
        self.retrieve_call = kwargs
        return self.records

    def close(self) -> None:
        self.closed = True


def make_record(chunk_id: str, *, rubric_id: str = "history-v1"):
    return SimpleNamespace(
        id=chunk_id,
        payload={
            "page_content": f"Content for {chunk_id}",
            "metadata": {
                "rubric_id": rubric_id,
                "document_id": "document-1",
                "chunk_index": int(chunk_id[-1]),
            },
        },
    )


def make_repository(client: FakeQdrantClient) -> QdrantRubricChunkRepository:
    return QdrantRubricChunkRepository(
        url="http://qdrant:6333",
        api_key=None,
        collection="rubric_chunks",
        client=client,
    )


def test_retrieve_uses_postgres_ids_and_preserves_their_order() -> None:
    client = FakeQdrantClient([make_record("chunk-2"), make_record("chunk-1")])
    repository = make_repository(client)

    chunks = repository.retrieve(
        chunk_ids=["chunk-1", "chunk-2"],
        rubric_id="history-v1",
        document_id="document-1",
    )

    assert [chunk.id for chunk in chunks] == ["chunk-1", "chunk-2"]
    assert client.retrieve_call == {
        "collection_name": "rubric_chunks",
        "ids": ["chunk-1", "chunk-2"],
        "with_payload": True,
        "with_vectors": False,
    }
    assert repository.health()


def test_retrieve_rejects_missing_postgres_referenced_chunk() -> None:
    repository = make_repository(FakeQdrantClient([make_record("chunk-1")]))

    with pytest.raises(QdrantPayloadError, match="missing 1 chunk"):
        repository.retrieve(
            chunk_ids=["chunk-1", "chunk-2"],
            rubric_id="history-v1",
            document_id="document-1",
        )


def test_retrieve_rejects_chunk_owned_by_another_rubric() -> None:
    repository = make_repository(
        FakeQdrantClient([make_record("chunk-1", rubric_id="another-rubric")])
    )

    with pytest.raises(QdrantPayloadError, match="another rubric"):
        repository.retrieve(
            chunk_ids=["chunk-1"],
            rubric_id="history-v1",
            document_id="document-1",
        )
