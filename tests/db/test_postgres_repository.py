import pytest
from sqlalchemy import create_engine, insert

from app.db import PostgresRubricMetadataRepository, RubricMetadataNotFoundError
from app.db.postgres_repository import metadata, rubrics


def make_repository() -> PostgresRubricMetadataRepository:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    metadata.create_all(engine)
    return PostgresRubricMetadataRepository(engine=engine)


def test_get_reads_rag_owned_chunk_metadata() -> None:
    repository = make_repository()
    with repository.engine.begin() as connection:
        connection.execute(
            insert(rubrics).values(
                id="history-v1",
                document_id="document-1",
                version="1",
                course_id="HIST-101",
                exam_id="history-midterm",
                processed=True,
                processing_status="completed",
                processing_error=None,
                archived=False,
                chunk_count=2,
                chunk_ids=["chunk-1", "chunk-2"],
            )
        )

    rubric = repository.get("history-v1")

    assert rubric.document_id == "document-1"
    assert rubric.exam_id == "history-midterm"
    assert rubric.chunk_ids == ["chunk-1", "chunk-2"]
    assert repository.health()


def test_get_reports_unknown_rubric() -> None:
    repository = make_repository()

    with pytest.raises(RubricMetadataNotFoundError):
        repository.get("missing")
