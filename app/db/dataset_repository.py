"""Version reservations, source reconciliation, and leased dataset publication.

Lock order: closed tests (sorted), dataset singleton, version. Storage transfers
happen outside transactions. A ready version and its object key never change.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    delete,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError

from app.db.grading_repository import (
    GradingConflictError,
    GradingRecordNotFoundError,
    attempts,
    criteria_met,
    grading_jobs,
    grading_metadata,
    responses,
    tests,
)
from app.db.grading_workflow import LeaseLostError
from app.model import Test
from app.model.assessment import utc_datetime
from app.service.training_csv import TrainingCsvError, encode_training_csv, validate_row

DATASET_ID = "training"

training_datasets = Table(
    "training_datasets",
    grading_metadata,
    Column("id", String(32), primary_key=True),
    Column("schema_version", Integer, nullable=False, default=1),
    Column("active_version_id", String(36), nullable=True),
    Column("next_number", Integer, nullable=False, default=1),
    Column("seed_rows", JSON, nullable=False),
    Column("source_fingerprint", String(64), nullable=False, default=""),
)
dataset_versions = Table(
    "training_dataset_versions",
    grading_metadata,
    Column("id", String(36), primary_key=True),
    Column(
        "dataset_id", ForeignKey("training_datasets.id"), nullable=False, index=True
    ),
    Column("number", Integer, nullable=False, unique=True),
    Column("schema_version", Integer, nullable=False, default=1),
    Column("parent_version_id", String(36), nullable=True),
    Column("source", String(32), nullable=False),
    Column("creator_id", String(128), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("ready_at", DateTime(timezone=True), nullable=True),
    Column("filename", String(255), nullable=True),
    Column("status", String(16), nullable=False, index=True),
    Column("request_key", String(256), nullable=False, unique=True),
    Column("input_checksum", String(64), nullable=True),
    Column("input_seed", JSON, nullable=True),
    Column("source_fingerprint", String(64), nullable=True),
    Column("source_manifest", JSON, nullable=True),
    Column("seed_snapshot", JSON, nullable=True),
    Column("object_key", String(512), nullable=True),
    Column("checksum", String(64), nullable=True),
    Column("row_count", Integer, nullable=True),
    Column("byte_size", Integer, nullable=True),
    Column("staged_keys", JSON, nullable=False),
    Column("claim_count", Integer, nullable=False, default=0),
    Column("next_retry_at", DateTime(timezone=True), nullable=False),
    Column("lease_token", String(36), nullable=True),
    Column("lease_expires_at", DateTime(timezone=True), nullable=True),
    Column("last_error", Text, nullable=True),
    Column("retry_of", String(36), nullable=True),
)
dataset_source_rows = Table(
    "training_dataset_source_rows",
    grading_metadata,
    Column("source_key", String(64), primary_key=True),
    Column("test_id", ForeignKey("grading_tests.id"), nullable=False, index=True),
    Column("attempt_id", String(36), nullable=False),
    Column("question_id", String(128), nullable=False),
    Column("submission_version", Integer, nullable=False),
    Column("grade_revision", Integer, nullable=False),
    Column("label_source", String(16), nullable=False),
    Column("row", JSON, nullable=False),
)
test_dataset_exports = Table(
    "training_test_exports",
    grading_metadata,
    Column("test_id", ForeignKey("grading_tests.id"), primary_key=True),
    Column("fingerprint", String(64), nullable=False),
    Column("status", String(24), nullable=False),
    Column("row_count", Integer, nullable=False),
    Column("excluded", JSON, nullable=False),
    Column("version_id", String(36), nullable=True),
    Column("last_error", Text, nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False
        ).encode()
    ).hexdigest()


VERSION_FIELDS = [
    "id",
    "number",
    "schema_version",
    "parent_version_id",
    "source",
    "creator_id",
    "created_at",
    "ready_at",
    "filename",
    "status",
    "checksum",
    "row_count",
    "byte_size",
    "last_error",
    "retry_of",
]


def version_metadata(row, active_id=None) -> dict:
    result = {key: row[key] for key in VERSION_FIELDS}
    result["is_active"] = row["id"] == active_id
    for key in ("created_at", "ready_at"):
        result[key] = utc_datetime(result[key])
    return result


class DatasetRepository:
    def __init__(self, store):
        self.store = store
        self.engine = store.engine

    def initialize(self):
        grading_metadata.create_all(self.engine)
        with self.engine.begin() as connection:
            try:
                with connection.begin_nested():
                    connection.execute(
                        insert(training_datasets).values(id=DATASET_ID, seed_rows=[])
                    )
            except IntegrityError:
                pass

    @staticmethod
    def _dataset(connection):
        return (
            connection.execute(
                select(training_datasets)
                .where(training_datasets.c.id == DATASET_ID)
                .with_for_update()
            )
            .mappings()
            .one()
        )

    def _reserve(self, connection, dataset, source, request_key, **values):
        existing = (
            connection.execute(
                select(dataset_versions).where(
                    dataset_versions.c.request_key == request_key
                )
            )
            .mappings()
            .first()
        )
        if existing is not None:
            if (
                "input_checksum" in values
                and existing["input_checksum"] != values["input_checksum"]
            ):
                raise GradingConflictError(
                    "This upload key was already used for a different CSV."
                )
            return dict(existing)
        version_id, now = str(uuid.uuid4()), datetime.now(UTC)
        connection.execute(
            insert(dataset_versions).values(
                id=version_id,
                dataset_id=DATASET_ID,
                number=dataset["next_number"],
                source=source,
                status="pending",
                request_key=request_key,
                created_at=now,
                next_retry_at=now,
                staged_keys=[],
                **values,
            )
        )
        connection.execute(
            update(training_datasets)
            .where(training_datasets.c.id == DATASET_ID)
            .values(next_number=dataset["next_number"] + 1)
        )
        return dict(
            connection.execute(
                select(dataset_versions).where(dataset_versions.c.id == version_id)
            )
            .mappings()
            .one()
        )

    @staticmethod
    def _latest_retry(connection, version):
        while version["status"] == "failed":
            retry = (
                connection.execute(
                    select(dataset_versions)
                    .where(dataset_versions.c.retry_of == version["id"])
                    .order_by(dataset_versions.c.number.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if retry is None:
                break
            version = retry
        return version

    def reserve_upload(self, rows, checksum, creator_id, filename, request_key):
        with self.engine.begin() as connection:
            dataset = self._dataset(connection)
            canonical_checksum = hashlib.sha256(encode_training_csv(rows)).hexdigest()
            previous_snapshot = connection.execute(
                select(dataset_versions.c.seed_snapshot)
                .where(
                    dataset_versions.c.status == "ready",
                    dataset_versions.c.checksum == canonical_checksum,
                )
                .order_by(dataset_versions.c.number.desc())
                .limit(1)
            ).scalar_one_or_none()
            if previous_snapshot is not None:
                rows = previous_snapshot
            result = self._reserve(
                connection,
                dataset,
                "instructor_upload",
                f"upload:{creator_id}:{request_key}",
                input_seed=rows,
                input_checksum=checksum,
                creator_id=creator_id,
                filename=filename,
            )
            return version_metadata(result, dataset["active_version_id"])

    @staticmethod
    def _training_row(question, response, labels):
        if not question.rubric:
            raise TrainingCsvError("No accepted rubric is available for this question.")
        criteria = question.rubric.criteria
        if not criteria:
            raise TrainingCsvError("The accepted rubric has no criteria.")
        overrides = response["criterion_scores"]
        if overrides is not None:
            scores = {item["criteria_id"]: item["score"] for item in overrides}
        else:
            matched = {item["criteria_id"]: item["is_met"] for item in labels}
            if set(matched) != {item.id for item in criteria} or len(labels) != len(
                criteria
            ):
                raise TrainingCsvError(
                    "Criterion labels are missing or duplicated. Correct criterion scores in submission review."
                )
            scores = {
                item.id: item.score if matched[item.id] else 0 for item in criteria
            }
        if set(scores) != {item.id for item in criteria}:
            raise TrainingCsvError("Criterion scores do not match the accepted rubric.")
        if not math.isclose(
            sum(scores.values()), response["score"], rel_tol=1e-9, abs_tol=1e-7
        ):
            raise TrainingCsvError(
                "Criterion scores do not match the current total. Correct criterion scores in submission review."
            )
        return validate_row(
            {
                "id": question.external_id or question.id,
                "question": question.prompt,
                "desired_answer": question.model_answer or "",
                "student_answer": response["answer"],
                "criteria[]": [item.description for item in criteria],
                "criteria_score[]": [scores[item.id] for item in criteria],
                "criteria_max_score[]": [item.score for item in criteria],
                "student_score": response["score"],
            }
        )

    def _collect(self, connection):
        """Capture closed-test sources under the same locks used by grading/editing."""
        closed = (
            connection.execute(
                select(tests)
                .where(
                    or_(
                        tests.c.ended_at.isnot(None),
                        tests.c.ends_at <= datetime.now(UTC),
                    )
                )
                .order_by(tests.c.id)
                .with_for_update()
            )
            .mappings()
            .all()
        )
        states = []
        for test in closed:
            submitted = (
                connection.execute(
                    select(attempts)
                    .where(
                        attempts.c.test_id == test["id"],
                        attempts.c.submitted_at.isnot(None),
                    )
                    .order_by(
                        attempts.c.submitted_at.desc(), attempts.c.attempt_number.desc()
                    )
                )
                .mappings()
                .all()
            )
            latest = {}
            for attempt in submitted:
                latest.setdefault(attempt["user_id"], attempt)
            fingerprint = digest(
                [
                    (
                        item["id"],
                        item["status"],
                        item["submission_version"],
                        item["revision"],
                    )
                    for item in sorted(
                        latest.values(), key=lambda value: value["user_id"]
                    )
                ]
            )
            waiting = any(item["status"] == "grading" for item in latest.values())
            previous = (
                connection.execute(
                    select(test_dataset_exports).where(
                        test_dataset_exports.c.test_id == test["id"]
                    )
                )
                .mappings()
                .first()
            )
            if previous is not None and previous["fingerprint"] == fingerprint:
                # Immutable accepted answers and revisioned edits let us reuse the source ledger.
                states.append(
                    {
                        "test_id": test["id"],
                        "fingerprint": fingerprint,
                        "waiting": waiting,
                        "rows": [],
                        "excluded": previous["excluded"],
                        "previous": previous,
                    }
                )
                continue
            rows, excluded = [], []
            if not waiting:
                for attempt in latest.values():
                    if attempt["status"] != "graded":
                        excluded.append(
                            {
                                "attempt_id": attempt["id"],
                                "question_id": None,
                                "reason": "Latest submission is not successfully graded.",
                            }
                        )
                        continue
                    snapshot = connection.execute(
                        select(grading_jobs.c.snapshot).where(
                            grading_jobs.c.attempt_id == attempt["id"],
                            grading_jobs.c.submission_version
                            == attempt["submission_version"],
                        )
                    ).scalar_one_or_none()
                    if snapshot is None:
                        excluded.append(
                            {
                                "attempt_id": attempt["id"],
                                "question_id": None,
                                "reason": "Accepted grading snapshot is missing.",
                            }
                        )
                        continue
                    saved = (
                        connection.execute(
                            select(responses).where(
                                responses.c.attempt_id == attempt["id"]
                            )
                        )
                        .mappings()
                        .all()
                    )
                    by_question = {item["question_id"]: item for item in saved}
                    accepted = Test.model_validate(snapshot)
                    if len(saved) != len(accepted.questions) or any(
                        item["score"] is None for item in saved
                    ):
                        excluded.append(
                            {
                                "attempt_id": attempt["id"],
                                "question_id": None,
                                "reason": "Latest submission is incomplete.",
                            }
                        )
                        continue
                    for question in accepted.questions:
                        response = by_question.get(question.id)
                        try:
                            if response is None:
                                raise TrainingCsvError("Submitted response is missing.")
                            labels = (
                                connection.execute(
                                    select(criteria_met).where(
                                        criteria_met.c.response_id == response["id"]
                                    )
                                )
                                .mappings()
                                .all()
                            )
                            training_row = self._training_row(
                                question, response, labels
                            )
                            rows.append(
                                {
                                    "source_key": digest(
                                        [test["id"], attempt["user_id"], question.id]
                                    ),
                                    "test_id": test["id"],
                                    "attempt_id": attempt["id"],
                                    "question_id": question.id,
                                    "submission_version": attempt["submission_version"],
                                    "grade_revision": attempt["revision"],
                                    "label_source": "instructor"
                                    if response["criterion_scores"] is not None
                                    else "llm",
                                    "row": training_row,
                                }
                            )
                        except TrainingCsvError as exc:
                            excluded.append(
                                {
                                    "attempt_id": attempt["id"],
                                    "question_id": question.id,
                                    "reason": str(exc),
                                }
                            )
            states.append(
                {
                    "test_id": test["id"],
                    "fingerprint": fingerprint,
                    "waiting": waiting,
                    "rows": rows,
                    "excluded": excluded,
                    "previous": previous,
                }
            )
        return states

    def _reconcile_locked(self, connection):
        states = self._collect(connection)
        dataset = self._dataset(connection)
        for state in states:
            previous = state["previous"]
            if previous is None or previous["fingerprint"] != state["fingerprint"]:
                if not state["waiting"]:
                    connection.execute(
                        delete(dataset_source_rows).where(
                            dataset_source_rows.c.test_id == state["test_id"]
                        )
                    )
                    if state["rows"]:
                        connection.execute(insert(dataset_source_rows), state["rows"])
                count = (
                    len(state["rows"])
                    if not state["waiting"]
                    else (previous["row_count"] if previous else 0)
                )
                values = {
                    "fingerprint": state["fingerprint"],
                    "status": "waiting"
                    if state["waiting"]
                    else "needs_attention"
                    if state["excluded"]
                    else "pending",
                    "row_count": count,
                    "excluded": state["excluded"],
                    "last_error": None,
                    "updated_at": datetime.now(UTC),
                }
                if previous:
                    connection.execute(
                        update(test_dataset_exports)
                        .where(test_dataset_exports.c.test_id == state["test_id"])
                        .values(**values)
                    )
                else:
                    connection.execute(
                        insert(test_dataset_exports).values(
                            test_id=state["test_id"], **values
                        )
                    )
        source_fingerprint = digest(
            [(state["test_id"], state["fingerprint"]) for state in states]
        )
        if source_fingerprint != dataset["source_fingerprint"]:
            connection.execute(
                update(training_datasets)
                .where(training_datasets.c.id == DATASET_ID)
                .values(source_fingerprint=source_fingerprint)
            )
        return {**dict(dataset), "source_fingerprint": source_fingerprint}

    @staticmethod
    def _rows(connection, seed):
        sources = (
            connection.execute(
                select(dataset_source_rows).order_by(dataset_source_rows.c.source_key)
            )
            .mappings()
            .all()
        )
        return seed + [item["row"] for item in sources], [
            {key: value for key, value in item.items() if key != "row"}
            for item in sources
        ]

    def reconcile(self):
        with self.engine.begin() as connection:
            dataset = self._reconcile_locked(connection)
            rows, _ = self._rows(connection, dataset["seed_rows"])
            if not rows and dataset["active_version_id"] is None:
                self._mark_exports(connection, None)
                return
            checksum = hashlib.sha256(encode_training_csv(rows)).hexdigest()
            active = (
                connection.execute(
                    select(dataset_versions).where(
                        dataset_versions.c.id == dataset["active_version_id"]
                    )
                )
                .mappings()
                .first()
            )
            if active and checksum == active["checksum"]:
                self._mark_exports(connection, active["id"])
                return
            key = f"export:{dataset['active_version_id'] or 'empty'}:{checksum}"
            version = self._latest_retry(
                connection, self._reserve(connection, dataset, "test_export", key)
            )
            connection.execute(
                update(test_dataset_exports)
                .where(
                    test_dataset_exports.c.status.in_(["pending", "needs_attention"])
                )
                .values(
                    version_id=version["id"],
                    last_error=version["last_error"]
                    if version["status"] == "failed"
                    else None,
                )
            )

    @staticmethod
    def _mark_exports(connection, version_id):
        for row in connection.execute(select(test_dataset_exports)).mappings().all():
            if row["status"] == "waiting":
                continue
            connection.execute(
                update(test_dataset_exports)
                .where(test_dataset_exports.c.test_id == row["test_id"])
                .values(
                    status="needs_attention" if row["excluded"] else "completed",
                    version_id=version_id,
                    last_error=None,
                )
            )

    def claim(self, lease_seconds, max_retries):
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            self._dataset(connection)
            row = (
                connection.execute(
                    select(dataset_versions)
                    .where(dataset_versions.c.status.in_(["pending", "running"]))
                    .order_by(dataset_versions.c.number)
                    .limit(1)
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if (
                row is None
                or (
                    row["status"] == "pending"
                    and utc_datetime(row["next_retry_at"]) > now
                )
                or (
                    row["status"] == "running"
                    and utc_datetime(row["lease_expires_at"]) > now
                )
            ):
                return None
            token = str(uuid.uuid4())
            connection.execute(
                update(dataset_versions)
                .where(dataset_versions.c.id == row["id"])
                .values(
                    status="running",
                    lease_token=token,
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    claim_count=row["claim_count"] + 1,
                )
            )
            return {
                **dict(row),
                "lease_token": token,
                "claim_count": row["claim_count"] + 1,
                "exhausted": row["claim_count"] > max_retries,
            }

    @staticmethod
    def _owned(connection, job):
        row = (
            connection.execute(
                select(dataset_versions)
                .where(dataset_versions.c.id == job["id"])
                .with_for_update()
            )
            .mappings()
            .one()
        )
        if (
            row["status"] != "running"
            or row["lease_token"] != job["lease_token"]
            or utc_datetime(row["lease_expires_at"]) <= datetime.now(UTC)
        ):
            raise LeaseLostError("Dataset publication lease expired.")
        return row

    def prepare(self, job):
        with self.engine.begin() as connection:
            dataset = self._reconcile_locked(connection)
            version = self._owned(connection, job)
            seed = (
                version["input_seed"]
                if version["source"] == "instructor_upload"
                else dataset["seed_rows"]
            )
            rows, manifest = self._rows(connection, seed)
            payload = encode_training_csv(rows)
            checksum = hashlib.sha256(payload).hexdigest()
            active_checksum = connection.execute(
                select(dataset_versions.c.checksum).where(
                    dataset_versions.c.id == dataset["active_version_id"]
                )
            ).scalar_one_or_none()
            if version["source"] == "test_export" and checksum == active_checksum:
                connection.execute(
                    update(dataset_versions)
                    .where(dataset_versions.c.id == job["id"])
                    .values(
                        status="unchanged",
                        lease_token=None,
                        lease_expires_at=None,
                        last_error=None,
                    )
                )
                self._mark_exports(connection, dataset["active_version_id"])
                return None
            key = f"training-datasets/{DATASET_ID}/versions/{job['id']}/{checksum}/dataset.csv"
            staged = list(dict.fromkeys([*version["staged_keys"], key]))
            values = {
                "parent_version_id": dataset["active_version_id"],
                "source_fingerprint": dataset["source_fingerprint"],
                "source_manifest": manifest,
                "seed_snapshot": seed,
                "object_key": key,
                "checksum": checksum,
                "row_count": len(rows),
                "byte_size": len(payload),
                "staged_keys": staged,
            }
            connection.execute(
                update(dataset_versions)
                .where(dataset_versions.c.id == job["id"])
                .values(**values)
            )
            connection.execute(
                update(test_dataset_exports)
                .where(
                    test_dataset_exports.c.status.in_(["pending", "needs_attention"])
                )
                .values(version_id=job["id"])
            )
            return {**dict(version), **values, "payload": payload}

    def activate(self, job, prepared):
        with self.engine.begin() as connection:
            dataset = self._reconcile_locked(connection)
            version = self._owned(connection, job)
            if (
                dataset["active_version_id"] != prepared["parent_version_id"]
                or dataset["source_fingerprint"] != prepared["source_fingerprint"]
            ):
                connection.execute(
                    update(dataset_versions)
                    .where(dataset_versions.c.id == job["id"])
                    .values(
                        status="pending",
                        lease_token=None,
                        lease_expires_at=None,
                        next_retry_at=datetime.now(UTC),
                        claim_count=0,
                        last_error="Source grades changed; rebuilding the CSV.",
                    )
                )
                return False
            # The token protects activation; checksum/key come from the persisted reservation.
            if version["checksum"] != prepared["checksum"]:
                raise LeaseLostError("Dataset reservation changed.")
            connection.execute(
                update(dataset_versions)
                .where(dataset_versions.c.id == job["id"])
                .values(
                    status="ready",
                    ready_at=datetime.now(UTC),
                    lease_token=None,
                    lease_expires_at=None,
                    last_error=None,
                )
            )
            values = {"active_version_id": job["id"]}
            if version["source"] == "instructor_upload":
                values["seed_rows"] = version["input_seed"]
            connection.execute(
                update(training_datasets)
                .where(training_datasets.c.id == DATASET_ID)
                .values(**values)
            )
            self._mark_exports(connection, job["id"])
            return True

    def renew(self, job, seconds):
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            result = connection.execute(
                update(dataset_versions)
                .where(
                    dataset_versions.c.id == job["id"],
                    dataset_versions.c.status == "running",
                    dataset_versions.c.lease_token == job["lease_token"],
                    dataset_versions.c.lease_expires_at > now,
                )
                .values(lease_expires_at=now + timedelta(seconds=seconds))
            )
            return result.rowcount == 1

    def fail(self, job, error, *, retry=False, delay=0):
        with self.engine.begin() as connection:
            self._dataset(connection)
            self._owned(connection, job)
            connection.execute(
                update(dataset_versions)
                .where(dataset_versions.c.id == job["id"])
                .values(
                    status="pending" if retry else "failed",
                    lease_token=None,
                    lease_expires_at=None,
                    next_retry_at=datetime.now(UTC) + timedelta(seconds=delay),
                    last_error=error,
                )
            )
            if not retry:
                connection.execute(
                    update(test_dataset_exports)
                    .where(
                        test_dataset_exports.c.version_id == job["id"],
                        test_dataset_exports.c.status.in_(
                            ["pending", "needs_attention"]
                        ),
                    )
                    .values(last_error=error)
                )

    def release(self, job):
        with self.engine.begin() as connection:
            connection.execute(
                update(dataset_versions)
                .where(
                    dataset_versions.c.id == job["id"],
                    dataset_versions.c.lease_token == job["lease_token"],
                    dataset_versions.c.status == "running",
                )
                .values(
                    status="pending",
                    lease_token=None,
                    lease_expires_at=None,
                    next_retry_at=datetime.now(UTC),
                )
            )

    def retry(self, version_id, creator_id):
        with self.engine.begin() as connection:
            dataset = self._dataset(connection)
            row = (
                connection.execute(
                    select(dataset_versions).where(dataset_versions.c.id == version_id)
                )
                .mappings()
                .first()
            )
            if row is None:
                raise GradingRecordNotFoundError(version_id)
            if row["status"] != "failed":
                return version_metadata(row, dataset["active_version_id"])
            while True:
                result = self._reserve(
                    connection,
                    dataset,
                    row["source"],
                    f"retry:{row['id']}",
                    input_seed=row["input_seed"],
                    input_checksum=row["input_checksum"],
                    filename=row["filename"],
                    creator_id=creator_id,
                    retry_of=row["id"],
                )
                if result["status"] != "failed":
                    connection.execute(
                        update(test_dataset_exports)
                        .where(test_dataset_exports.c.version_id == version_id)
                        .values(version_id=result["id"], last_error=None)
                    )
                    return version_metadata(result, dataset["active_version_id"])
                # Retrying a failed retry reserves another number, preserving history.
                row = result

    def list_versions(self):
        with self.engine.connect() as connection:
            active = connection.execute(
                select(training_datasets.c.active_version_id).where(
                    training_datasets.c.id == DATASET_ID
                )
            ).scalar_one()
            rows = (
                connection.execute(
                    select(
                        *(dataset_versions.c[name] for name in VERSION_FIELDS),
                        dataset_versions.c.object_key,
                    ).order_by(dataset_versions.c.number.desc())
                )
                .mappings()
                .all()
            )
        return [version_metadata(row, active) for row in rows]

    def get_version(self, version_id=None):
        with self.engine.connect() as connection:
            active = connection.execute(
                select(training_datasets.c.active_version_id).where(
                    training_datasets.c.id == DATASET_ID
                )
            ).scalar_one()
            row = (
                connection.execute(
                    select(
                        *(dataset_versions.c[name] for name in VERSION_FIELDS),
                        dataset_versions.c.object_key,
                    ).where(dataset_versions.c.id == (version_id or active))
                )
                .mappings()
                .first()
            )
        if row is None:
            raise GradingRecordNotFoundError(version_id or "latest dataset")
        return dict(row), version_metadata(row, active)

    def export_status(self, test_id):
        with self.engine.connect() as connection:
            test = (
                connection.execute(select(tests).where(tests.c.id == test_id))
                .mappings()
                .first()
            )
            if test is None:
                raise GradingRecordNotFoundError(test_id)
            row = (
                connection.execute(
                    select(test_dataset_exports).where(
                        test_dataset_exports.c.test_id == test_id
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return {
                    "test_id": test_id,
                    "status": "pending"
                    if test["ended_at"]
                    or (
                        test["ends_at"]
                        and utc_datetime(test["ends_at"]) <= datetime.now(UTC)
                    )
                    else "open",
                    "row_count": 0,
                    "excluded": [],
                    "version_id": None,
                    "last_error": None,
                }
            result = dict(row)
            if result["last_error"]:
                result["status"] = "failed"
            elif result["status"] == "pending":
                result["status"] = "exporting"
            result["updated_at"] = utc_datetime(result["updated_at"])
            return result
