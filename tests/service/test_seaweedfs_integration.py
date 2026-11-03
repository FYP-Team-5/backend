"""Opt-in tests against the pinned SeaweedFS container.

DATASET_TEST_S3_ENDPOINT must point at an isolated test instance. Each test
uses a fresh bucket and deletes only objects it created.
"""

import hashlib
import io
import os
import uuid
from pathlib import Path

import pytest

from app.config import Settings
from app.service.dataset_storage import DatasetStorage, DatasetStorageError
from app.service.training_csv import encode_training_csv, parse_training_csv

pytestmark = pytest.mark.skipif(
    not os.getenv("DATASET_TEST_S3_ENDPOINT"),
    reason="SeaweedFS test endpoint is not configured",
)


@pytest.fixture
def storage():
    settings = Settings(
        dataset_s3_endpoint=os.environ["DATASET_TEST_S3_ENDPOINT"],
        dataset_s3_bucket="fyp-test-" + uuid.uuid4().hex,
        dataset_s3_access_key=os.getenv(
            "DATASET_TEST_S3_ACCESS_KEY", "fyp-integration"
        ),
        dataset_s3_secret_key=os.getenv(
            "DATASET_TEST_S3_SECRET_KEY", "isolated-integration-secret"
        ),
    )
    adapter = DatasetStorage(settings)
    adapter.ensure_bucket()
    try:
        yield adapter
    finally:
        objects = adapter.client.list_objects_v2(Bucket=adapter.bucket).get(
            "Contents", []
        )
        for item in objects:
            adapter.client.delete_object(Bucket=adapter.bucket, Key=item["Key"])
        adapter.client.delete_bucket(Bucket=adapter.bucket)
        adapter.close()


def test_seaweedfs_immutable_csv_upload_download(storage):
    reference = (
        Path(__file__).resolve().parents[3]
        / "qlora/Datasets/training_data_moh_eng_rubrics.csv"
    )
    path = (
        reference
        if reference.exists()
        else Path(__file__).resolve().parents[1] / "fixtures/training_dataset.csv"
    )
    with path.open("rb") as source:
        rows = parse_training_csv(source)
    payload = encode_training_csv(rows)
    checksum = hashlib.sha256(payload).hexdigest()
    storage.put_csv(
        "versions/one/dataset.csv", io.BytesIO(payload), checksum, len(payload)
    )
    # A repeated transfer verifies the existing immutable object's actual bytes.
    storage.put_csv(
        "versions/one/dataset.csv", io.BytesIO(payload), checksum, len(payload)
    )
    with storage.download_csv(
        "versions/one/dataset.csv", checksum, len(payload)
    ) as source:
        assert parse_training_csv(source) == rows
    different = payload + b"\n"
    with pytest.raises(DatasetStorageError):
        storage.put_csv(
            "versions/one/dataset.csv",
            io.BytesIO(different),
            hashlib.sha256(different).hexdigest(),
            len(different),
        )
    with storage.download_csv(
        "versions/one/dataset.csv", checksum, len(payload)
    ) as source:
        assert source.read() == payload


def test_seaweedfs_download_validates_actual_bytes(storage):
    payload = b"test csv bytes"
    checksum = hashlib.sha256(payload).hexdigest()
    storage.put_csv("verified.csv", io.BytesIO(payload), checksum, len(payload))
    storage.client.put_object(
        Bucket=storage.bucket,
        Key="verified.csv",
        Body=b"corrupted bytes",
        Metadata={"sha256": checksum},
    )
    with pytest.raises(DatasetStorageError):
        storage.download_csv("verified.csv", checksum, len(payload))
