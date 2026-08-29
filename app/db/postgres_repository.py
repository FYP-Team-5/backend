from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    func,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from app.model import RubricMetadata

metadata = MetaData()

# This is a read-only view of the columns owned by the RAG service. The grading
# service never creates or migrates the shared table.
rubrics = Table(
    "rubrics",
    metadata,
    Column("id", String(128), primary_key=True),
    Column("document_id", String(36), nullable=False),
    Column("version", String(64), nullable=False),
    Column("course_id", String(128), nullable=True),
    Column("exam_id", String(128), nullable=True),
    Column("processed", Boolean, nullable=False),
    Column("processing_status", String(32), nullable=False),
    Column("processing_error", Text, nullable=True),
    Column("archived", Boolean, nullable=False),
    Column("chunk_count", Integer, nullable=False),
    Column("chunk_ids", JSON, nullable=False),
)


class MetadataStoreError(RuntimeError):
    """Raised when shared rubric metadata cannot be read."""


class RubricMetadataNotFoundError(KeyError):
    pass


class PostgresRubricMetadataRepository:
    def __init__(
        self,
        database_url: str | None = None,
        *,
        engine: Engine | None = None,
    ) -> None:
        if engine is None and database_url is None:
            raise ValueError("database_url is required when engine is not provided.")
        self.engine = engine or create_engine(database_url, pool_pre_ping=True)

    def close(self) -> None:
        self.engine.dispose()

    def health(self) -> bool:
        try:
            with self.engine.connect() as connection:
                connection.execute(select(func.count()).select_from(rubrics))
            return True
        except SQLAlchemyError:
            return False

    def get(self, rubric_id: str) -> RubricMetadata:
        statement = select(rubrics).where(rubrics.c.id == rubric_id)
        try:
            with self.engine.connect() as connection:
                row = connection.execute(statement).mappings().first()
        except SQLAlchemyError as exc:
            raise MetadataStoreError(
                "Unable to read rubric metadata from PostgreSQL."
            ) from exc
        if row is None:
            raise RubricMetadataNotFoundError(rubric_id)
        try:
            return RubricMetadata.model_validate(dict(row))
        except ValueError as exc:
            raise MetadataStoreError("Stored rubric metadata is invalid.") from exc
