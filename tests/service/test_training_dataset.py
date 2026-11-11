import asyncio
import csv
import hashlib
import io
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, update

from app.config import Settings
from app.db import GradingConflictError, PostgresGradingRepository
from app.db.dataset_repository import (
    dataset_source_rows,
    dataset_versions,
)
from app.db.dataset_repository import (
    test_dataset_exports as export_records,
)
from app.db.grading_repository import tests
from app.db.grading_workflow import LeaseLostError
from app.dto import (
    FeedbackResult,
    GradeAttemptRequest,
    SubmissionGradeUpdate,
    TestCreate,
)
from app.service import GradingService
from app.service.dataset_service import DatasetService
from app.service.dataset_storage import DatasetStorageError
from app.service.training_csv import (
    COLUMNS,
    LEGACY_COLUMNS,
    TrainingCsvError,
    encode_training_csv,
    parse_training_csv,
    validate_row,
)
from tests.dataset_fakes import MemoryDatasetStorage

REFERENCE = (
    Path(__file__).resolve().parents[3]
    / "qlora/Datasets/training_data_moh_eng_rubrics.csv"
)


def seed_row(answer="Seed answer"):
    return {
        "id": "1.1",
        "question": 'Explain "prototypes", please.\nGive an example.',
        "desired_answer": "Model answer",
        "student_answer": answer,
        "criteria[]": ["Correct explanation"],
        "criteria_score[]": [1],
        "criteria_max_score[]": [1],
        "student_score": 1,
        "feedback": "Seed feedback",
    }


class Grader:
    async def grade_binary(self, **kwargs):
        return [1] + [0] * (len(kwargs["criteria"]) - 1)

    async def generate_feedback(self, **kwargs):
        return FeedbackResult(feedback="Feedback")

    async def close(self):
        pass


@pytest.fixture
def dataset(tmp_path):
    settings = Settings(
        dataset_retry_seconds=0, dataset_max_retries=1, dataset_poll_seconds=0.01
    )
    store = PostgresGradingRepository(
        engine=create_engine(f"sqlite+pysqlite:///{tmp_path / 'dataset.db'}")
    )
    store.initialize()
    storage = MemoryDatasetStorage()
    service = DatasetService(settings, store, storage=storage)
    service.repository.initialize()
    grader = GradingService(settings, grading_store=store, llm_client=Grader())
    course = store.create_course("CS", "Computer Science")
    test = store.create_test(
        course.id,
        TestCreate(
            test_name="Dataset quiz",
            ends_at=datetime.now(UTC) + timedelta(days=1),
            max_attempts=5,
            questions=[
                {
                    "external_id": "1.1",
                    "prompt": "Explain prototypes",
                    "model_answer": "Model answer",
                    "max_score": 4,
                    "score_increment": 1,
                    "rubric": {
                        "criteria": [
                            {"description": "Accuracy", "score": 2},
                            {"description": "Evidence", "score": 2},
                        ]
                    },
                }
            ],
        ),
    )
    yield service, grader, test, storage
    store.close()


def upload(service, rows=None, key=None):
    return asyncio.run(
        service.upload(
            io.BytesIO(encode_training_csv(rows if rows is not None else [seed_row()])),
            "instructor",
            "dataset.csv",
            key,
        )
    )


def drain(service):
    async def run():
        for _ in range(15):
            if not await service.process_pending():
                return
        raise AssertionError("Dataset worker did not settle")

    asyncio.run(run())


def submit(grader, test, student="student", answer="Student answer", finish=True):
    async def run():
        attempt = await grader.create_attempt(test.id, student)
        await grader.grade_attempt(
            test.id,
            attempt.id,
            student,
            GradeAttemptRequest(
                responses=[{"question_id": test.questions[0].id, "answer": answer}]
            ),
        )
        if finish:
            await grader.process_pending_job()
        return grader.grading_store.get_attempt(attempt.id)

    return asyncio.run(run())


def download(service, version=None):
    source, metadata = asyncio.run(service.download(version))
    with source:
        return parse_training_csv(source), metadata


def test_reference_csv_round_trip_retains_all_examples_and_arrays():
    path = (
        REFERENCE
        if REFERENCE.exists()
        else Path(__file__).resolve().parents[1] / "fixtures/training_dataset.csv"
    )
    with path.open("rb") as source:
        rows = parse_training_csv(source)
    assert len(rows) == (3778 if path == REFERENCE else 3)
    assert len({row["id"] for row in rows}) < len(rows)
    encoded = encode_training_csv(rows)
    assert encoded.decode().splitlines()[0].split(",") == COLUMNS
    assert parse_training_csv(io.BytesIO(encoded)) == rows
    special = [seed_row(), seed_row()]
    assert (
        parse_training_csv(io.BytesIO(b"\xef\xbb\xbf" + encode_training_csv(special)))
        == special
    )


def legacy_csv(rows):
    """Encode the published format used before feedback targets were added."""
    current = list(csv.DictReader(io.StringIO(encode_training_csv(rows).decode())))
    target = io.StringIO(newline="")
    writer = csv.DictWriter(target, fieldnames=LEGACY_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows({key: row[key] for key in LEGACY_COLUMNS} for row in current)
    return target.getvalue().encode()


def test_legacy_csv_imports_with_blank_feedback_and_exports_new_header():
    rows = parse_training_csv(io.BytesIO(legacy_csv([seed_row()])))
    assert rows[0]["feedback"] == ""
    assert encode_training_csv(rows).decode().splitlines()[0].split(",") == COLUMNS
    assert parse_training_csv(io.BytesIO(encode_training_csv(rows))) == rows


def test_feedback_survives_csv_quotes_newlines_and_unicode():
    row = {**seed_row(), "feedback": 'Try "abstraction", then explain reuse.\n很好。'}
    assert parse_training_csv(io.BytesIO(encode_training_csv([row]))) == [row]


def test_saved_seed_without_feedback_exports_a_blank_target():
    row = seed_row()
    row.pop("feedback")
    assert (
        parse_training_csv(io.BytesIO(encode_training_csv([row])))[0]["feedback"] == ""
    )


@pytest.mark.parametrize(
    "column,value",
    [
        ("criteria[]", "__import__('os').system('false')"),
        ("criteria_score[]", [True]),
        ("criteria_score[]", [float("nan")]),
        ("criteria_score[]", [2]),
        ("criteria_max_score[]", [0]),
        ("student_score", 0),
        ("student_score", float("inf")),
        ("criteria[]", ["A", "B"]),
        ("question", "  "),
        ("feedback", None),
        ("feedback", 123),
    ],
)
def test_invalid_training_rows_are_rejected(column, value):
    with pytest.raises(TrainingCsvError):
        validate_row({**seed_row(), column: value})


def test_uploaded_versions_are_immutable_and_idempotent(dataset):
    service, _, _, storage = dataset
    first = upload(service, key="request-one")
    assert upload(service, key="request-one")["id"] == first["id"]
    with pytest.raises(GradingConflictError):
        upload(service, [seed_row("Different")], "request-one")
    with pytest.raises(GradingConflictError):
        download(service, first["id"])
    drain(service)
    old, metadata = download(service, first["id"])
    second = upload(service, [seed_row("New seed")], "request-two")
    drain(service)
    current, active = download(service)
    assert old == [seed_row()]
    assert current[0]["student_answer"] == "New seed"
    assert active["id"] == second["id"]
    assert download(service, first["id"])[0] == old
    assert metadata["checksum"] != active["checksum"]
    assert len(storage.objects) == 2


def test_closed_test_data_appends_once_and_uploads_preserve_generated_rows(dataset):
    service, grader, test, _ = dataset
    first = upload(service)
    drain(service)
    submit(grader, test, student="one")
    submit(grader, test, student="two", answer="Student answer")
    drain(service)
    assert len(download(service)[0]) == 1  # test still open
    grader.workflow.end_test(test.id, "instructor")
    drain(service)
    rows, active = download(service)
    assert len(rows) == 3  # identical answers from two students remain distinct
    assert len({row["id"] for row in rows}) == 1
    assert rows[1]["criteria_score[]"] == [2, 0]
    assert rows[0]["feedback"] == "Seed feedback"
    assert all(row["feedback"] == "Feedback" for row in rows[1:])
    assert active["schema_version"] == 2
    count = len(asyncio.run(service.list_versions()))
    drain(service)
    assert len(asyncio.run(service.list_versions())) == count
    assert download(service, first["id"])[0] == [seed_row()]
    upload(service, [seed_row("Replacement seed")])
    drain(service)
    new_rows, _ = download(service)
    assert len(new_rows) == 3 and new_rows[0]["student_answer"] == "Replacement seed"
    # Re-uploading an exact published snapshot must not convert its generated rows into duplicate seed data.
    upload(service, new_rows, key="reupload-download")
    drain(service)
    assert len(download(service)[0]) == 3
    assert active["status"] == "ready"


def test_reupload_legacy_snapshot_preserves_seed_without_duplicating_test_rows(dataset):
    service, grader, test, storage = dataset
    upload(service)
    drain(service)
    submit(grader, test)
    grader.workflow.end_test(test.id, "instructor")
    drain(service)
    rows, metadata = download(service)
    old_payload = legacy_csv(rows)
    checksum = hashlib.sha256(old_payload).hexdigest()
    raw, _ = service.repository.get_version(metadata["id"])
    # Recreate an immutable version published by the previous eight-column writer.
    old_key = raw["object_key"].replace(raw["checksum"], checksum)
    storage.objects[old_key] = old_payload
    with service.repository.engine.begin() as connection:
        connection.execute(
            update(dataset_versions)
            .where(dataset_versions.c.id == metadata["id"])
            .values(
                checksum=checksum,
                object_key=old_key,
                byte_size=len(old_payload),
                schema_version=1,
            )
        )
    asyncio.run(service.upload(io.BytesIO(old_payload), "instructor", "legacy.csv"))
    drain(service)
    assert len(download(service)[0]) == 2
    source, _ = asyncio.run(service.download(metadata["id"]))
    with source:
        assert source.read() == old_payload


def test_schema_upgrade_refreshes_feedback_in_existing_closed_test_ledger(dataset):
    service, grader, test, _ = dataset
    submit(grader, test)
    grader.workflow.end_test(test.id, "instructor")
    drain(service)
    with service.repository.engine.begin() as connection:
        source = connection.execute(select(dataset_source_rows)).mappings().one()
        legacy_row = dict(source["row"])
        legacy_row.pop("feedback")
        connection.execute(update(dataset_source_rows).values(row=legacy_row))
        connection.execute(
            update(export_records).values(fingerprint="legacy-fingerprint")
        )
    drain(service)
    with service.repository.engine.connect() as connection:
        refreshed = connection.execute(select(dataset_source_rows)).mappings().one()
    assert refreshed["row"]["feedback"] == "Feedback"


def test_merged_export_is_validated_before_publication(dataset):
    service, grader, test, _ = dataset
    upload(service)
    drain(service)
    submit(grader, test)
    grader.workflow.end_test(test.id, "instructor")
    service.repository.reconcile()
    # A source row that violates the reference contract must block publication.
    with service.repository.engine.begin() as connection:
        source = connection.execute(select(dataset_source_rows)).mappings().one()
        corrupted = dict(source["row"])
        corrupted["student_score"] = 99
        connection.execute(
            update(dataset_source_rows)
            .where(dataset_source_rows.c.source_key == source["source_key"])
            .values(row=corrupted)
        )
    job = service.repository.claim(60, 3)
    assert job is not None and job["source"] == "test_export"
    with pytest.raises(TrainingCsvError, match="Published CSV failed validation"):
        service.repository.prepare(job)


def test_deadline_collection_bootstraps_without_page_traffic(dataset):
    service, grader, test, _ = dataset
    submit(grader, test)
    with grader.grading_store.engine.begin() as connection:
        connection.execute(
            update(tests).where(tests.c.id == test.id).values(ends_at=datetime.now(UTC))
        )
    drain(service)
    rows, _ = download(service)
    assert len(rows) == 1 and rows[0]["student_answer"] == "Student answer"
    assert asyncio.run(service.export_status(test.id))["status"] == "completed"


def test_latest_pending_submission_waits_instead_of_exporting_older_grade(dataset):
    service, grader, test, _ = dataset
    older = submit(grader, test, answer="Old answer")
    latest = submit(grader, test, answer="Latest answer", finish=False)
    grader.workflow.end_test(test.id, "instructor")
    drain(service)
    assert asyncio.run(service.export_status(test.id))["status"] == "waiting"
    assert asyncio.run(service.list_versions()) == []
    asyncio.run(grader.process_pending_job())
    drain(service)
    assert download(service)[0][0]["student_answer"] == "Latest answer"
    assert older.id != latest.id


def test_failed_latest_is_excluded_and_later_success_is_collected(dataset):
    service, grader, test, _ = dataset
    submit(grader, test, answer="Old answer")
    latest = submit(grader, test, answer="Retry answer", finish=False)
    job = grader.workflow.claim_job(60, 3)
    grader.workflow.finish_job(job, error="Temporary grading outage")
    grader.workflow.end_test(test.id, "instructor")
    drain(service)
    assert asyncio.run(service.list_versions()) == []
    assert asyncio.run(service.export_status(test.id))["excluded"]
    asyncio.run(grader.retry_grading(test.id, latest.id, "student"))
    asyncio.run(grader.process_pending_job())
    drain(service)
    assert download(service)[0][0]["student_answer"] == "Retry answer"


def test_instructor_corrections_publish_new_rows_and_keep_old_versions(dataset):
    service, grader, test, _ = dataset
    attempt = submit(grader, test)
    grader.workflow.end_test(test.id, "instructor")
    drain(service)
    previous, old = download(service)
    result = asyncio.run(grader.get_submission(test.id, attempt.id))
    payload = {
        "response_id": result.responses[0].id,
        "score": 3,
        "feedback": "Corrected",
    }
    # An aggregate edit cannot silently invent criterion labels.
    asyncio.run(
        grader.edit_submission(
            test.id,
            attempt.id,
            "instructor",
            SubmissionGradeUpdate(
                revision=result.attempt.revision, responses=[payload]
            ),
        )
    )
    drain(service)
    assert asyncio.run(service.export_status(test.id))["status"] == "needs_attention"
    result = asyncio.run(grader.get_submission(test.id, attempt.id))
    payload["criterion_scores"] = [
        {"criteria_id": test.questions[0].rubric.criteria[0].id, "score": 1},
        {"criteria_id": test.questions[0].rubric.criteria[1].id, "score": 2},
    ]
    asyncio.run(
        grader.edit_submission(
            test.id,
            attempt.id,
            "instructor",
            SubmissionGradeUpdate(
                revision=result.attempt.revision, responses=[payload]
            ),
        )
    )
    drain(service)
    rows, current = download(service)
    assert len(rows) == 1 and rows[0]["student_score"] == 3
    assert rows[0]["criteria_score[]"] == [1, 2]
    assert download(service, old["id"])[0] == previous
    assert current["id"] != old["id"]


def test_storage_failure_is_bounded_and_does_not_change_active_version(dataset):
    service, grader, test, storage = dataset
    first = upload(service)
    drain(service)
    storage.fail = True
    failed = upload(service, [seed_row("New")], "new-file")
    drain(service)
    versions = asyncio.run(service.list_versions())
    assert (
        next(item for item in versions if item["id"] == failed["id"])["status"]
        == "failed"
    )
    assert next(item for item in versions if item["is_active"])["id"] == first["id"]
    assert submit(grader, test).status == "graded"
    storage.fail = False
    retry = asyncio.run(service.retry(failed["id"], "instructor"))
    assert asyncio.run(service.retry(failed["id"], "instructor"))["id"] == retry["id"]
    drain(service)
    assert download(service)[0][0]["student_answer"] == "New"


def test_changed_grades_during_transfer_are_rebuilt_before_activation(dataset):
    service, grader, test, storage = dataset
    attempt = submit(grader, test)
    grader.workflow.end_test(test.id, "instructor")
    result = asyncio.run(grader.get_submission(test.id, attempt.id))

    def edit():
        grader.workflow.edit_grades(
            test.id,
            attempt.id,
            "instructor",
            SubmissionGradeUpdate(
                revision=result.attempt.revision,
                responses=[
                    {
                        "response_id": result.responses[0].id,
                        "score": 4,
                        "feedback": "Revised during transfer",
                        "criterion_scores": [
                            {"criteria_id": item.id, "score": 2}
                            for item in test.questions[0].rubric.criteria
                        ],
                    }
                ],
            ),
        )

    storage.after_put = edit
    drain(service)
    rows, _ = download(service)
    assert rows[0]["student_score"] == 4
    assert len(storage.objects) == 1  # obsolete staged CSV was cleaned up


def test_restart_recovers_uploaded_object_and_rejects_stale_worker(dataset):
    service, _, _, storage = dataset
    upload(service)
    abandoned = service.repository.claim(60, 3)
    prepared = service.repository.prepare(abandoned)
    storage.put_csv(
        prepared["object_key"],
        io.BytesIO(prepared["payload"]),
        prepared["checksum"],
        prepared["byte_size"],
    )
    with service.repository.engine.begin() as connection:
        connection.execute(
            update(dataset_versions).values(
                lease_expires_at=datetime.now(UTC) - timedelta(seconds=1)
            )
        )
    restarted = DatasetService(
        service.settings, service.repository.store, storage=storage
    )
    drain(restarted)
    assert len(storage.objects) == 1
    assert download(restarted)[0] == [seed_row()]
    with pytest.raises(LeaseLostError):
        service.repository.activate(abandoned, prepared)
    with pytest.raises(LeaseLostError):
        service.repository.fail(abandoned, "stale failure")


def test_missing_rubric_labels_are_reported_without_fabrication(dataset):
    service, grader, test, _ = dataset
    attempt = submit(grader, test)
    from app.db.grading_repository import criteria_met

    with grader.grading_store.engine.begin() as connection:
        connection.execute(criteria_met.delete())
    grader.workflow.end_test(test.id, "instructor")
    drain(service)
    status = asyncio.run(service.export_status(test.id))
    assert status["status"] == "needs_attention"
    assert "missing" in status["excluded"][0]["reason"]
    assert asyncio.run(service.list_versions()) == []
    assert grader.grading_store.get_attempt(attempt.id).status == "graded"


def test_download_detects_corrupted_object(dataset):
    service, _, _, storage = dataset
    reserved = upload(service)
    drain(service)
    raw, _ = service.repository.get_version(reserved["id"])
    storage.objects[raw["object_key"]] = b"corrupted"
    with pytest.raises(DatasetStorageError):
        download(service)


def test_feedback_only_edits_publish_new_version_and_keep_label_provenance(dataset):
    from app.db.dataset_repository import dataset_source_rows

    service, grader, test, _ = dataset
    attempt = submit(grader, test)
    grader.workflow.end_test(test.id, "instructor")
    drain(service)
    before = len(service.repository.list_versions())
    old_rows, old = download(service)
    result = asyncio.run(grader.get_submission(test.id, attempt.id))
    asyncio.run(
        grader.edit_submission(
            test.id,
            attempt.id,
            "instructor",
            SubmissionGradeUpdate(
                revision=result.attempt.revision,
                responses=[
                    {
                        "response_id": result.responses[0].id,
                        "score": 2,
                        "feedback": "Clarified feedback only",
                    }
                ],
            ),
        )
    )
    drain(service)
    assert len(service.repository.list_versions()) == before + 1
    new_rows, current = download(service)
    assert new_rows[0]["feedback"] == "Clarified feedback only"
    assert new_rows[0]["criteria_score[]"] == old_rows[0]["criteria_score[]"]
    assert current["checksum"] != old["checksum"]
    assert download(service, old["id"])[0][0]["feedback"] == "Feedback"
    with service.repository.engine.connect() as connection:
        row = connection.execute(select(dataset_source_rows)).mappings().one()
    assert row["label_source"] == "llm"
    assert row["grade_revision"] > result.attempt.revision


def test_export_retry_reports_current_reservation_during_reconciliation(dataset):
    service, grader, test, storage = dataset
    service.settings.dataset_max_retries = 0
    submit(grader, test)
    grader.workflow.end_test(test.id, "instructor")
    storage.fail = True
    drain(service)
    failed = asyncio.run(service.export_status(test.id))
    assert failed["status"] == "failed"
    storage.fail = False
    retried = asyncio.run(service.retry_export(test.id, "instructor"))
    assert retried["status"] == "exporting"
    assert retried["version_id"] != failed["version_id"]
    service.repository.reconcile()
    pending = asyncio.run(service.export_status(test.id))
    assert pending["status"] == "exporting"
    assert pending["version_id"] == retried["version_id"]
    drain(service)
    assert asyncio.run(service.export_status(test.id))["status"] == "completed"
    assert len(download(service)[0]) == 1


def test_source_changes_do_not_consume_storage_failure_retry_budget(dataset):
    service, grader, test, storage = dataset
    service.settings.dataset_max_retries = 0
    attempt = submit(grader, test)
    upload(service)
    grader.workflow.end_test(test.id, "instructor")
    result = asyncio.run(grader.get_submission(test.id, attempt.id))

    def edit():
        grader.workflow.edit_grades(
            test.id,
            attempt.id,
            "instructor",
            SubmissionGradeUpdate(
                revision=result.attempt.revision,
                responses=[
                    {
                        "response_id": result.responses[0].id,
                        "score": 4,
                        "feedback": "Corrected",
                        "criterion_scores": [
                            {"criteria_id": criterion.id, "score": 2}
                            for criterion in test.questions[0].rubric.criteria
                        ],
                    }
                ],
            ),
        )

    storage.after_put = edit
    drain(service)
    rows, _ = download(service)
    assert len(rows) == 2
    assert rows[1]["student_score"] == 4
    assert all(
        version["status"] != "failed" for version in service.repository.list_versions()
    )
    assert asyncio.run(service.export_status(test.id))["status"] == "completed"


def test_unchanged_closed_tests_reuse_the_ledger_and_grade_edits_invalidate_it(
    dataset, monkeypatch
):
    service, grader, test, _ = dataset
    attempt = submit(grader, test)
    grader.workflow.end_test(test.id, "instructor")
    drain(service)
    original = service.repository._training_row

    def unexpected_read(*args):
        raise AssertionError(
            "Unchanged submissions should reuse their validated ledger rows"
        )

    monkeypatch.setattr(service.repository, "_training_row", unexpected_read)
    service.repository.reconcile()
    assert len(download(service)[0]) == 1
    monkeypatch.setattr(service.repository, "_training_row", original)
    result = asyncio.run(grader.get_submission(test.id, attempt.id))
    asyncio.run(
        grader.edit_submission(
            test.id,
            attempt.id,
            "instructor",
            SubmissionGradeUpdate(
                revision=result.attempt.revision,
                responses=[
                    {
                        "response_id": result.responses[0].id,
                        "score": 4,
                        "feedback": "Corrected",
                        "criterion_scores": [
                            {"criteria_id": criterion.id, "score": 2}
                            for criterion in test.questions[0].rubric.criteria
                        ],
                    }
                ],
            ),
        )
    )
    drain(service)
    assert download(service)[0][0]["student_score"] == 4
