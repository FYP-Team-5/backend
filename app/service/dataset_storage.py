"""S3-compatible, immutable CSV object transfers to SeaweedFS."""

from __future__ import annotations

import hashlib
import tempfile

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError


class DatasetStorageError(RuntimeError):
    pass


class DatasetStorage:
    def __init__(self, settings, *, client=None):
        self.bucket = settings.dataset_s3_bucket
        self.client = client or boto3.client(
            "s3",
            endpoint_url=settings.dataset_s3_endpoint,
            aws_access_key_id=settings.dataset_s3_access_key,
            aws_secret_access_key=settings.dataset_s3_secret_key,
            region_name=settings.dataset_s3_region,
            config=Config(
                connect_timeout=3,
                read_timeout=20,
                retries={"max_attempts": 2, "mode": "standard"},
                s3={"addressing_style": "path"},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )

    def ensure_bucket(self):
        try:
            try:
                self.client.head_bucket(Bucket=self.bucket)
            except ClientError as exc:
                if str(exc.response["Error"]["Code"]) not in {
                    "404",
                    "NoSuchBucket",
                    "NotFound",
                }:
                    raise
                try:
                    self.client.create_bucket(Bucket=self.bucket)
                except ClientError as create_exc:
                    if create_exc.response["Error"]["Code"] not in {
                        "BucketAlreadyOwnedByYou",
                        "BucketAlreadyExists",
                    }:
                        raise
        except (BotoCoreError, ClientError) as exc:
            raise DatasetStorageError(
                "Training dataset storage is unavailable."
            ) from exc

    def put_csv(self, key: str, source, checksum: str, size: int):
        try:
            source.seek(0)
            try:
                self.client.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=source,
                    ContentLength=size,
                    ContentType="text/csv; charset=utf-8",
                    Metadata={"sha256": checksum},
                    IfNoneMatch="*",
                )
            except ClientError as exc:
                if str(exc.response["Error"]["Code"]) not in {
                    "PreconditionFailed",
                    "412",
                }:
                    raise
            # Verify actual bytes, not only caller-supplied object metadata.
            checked = self.download_csv(key, checksum, size)
            checked.close()
        except (BotoCoreError, ClientError) as exc:
            raise DatasetStorageError("Could not publish the training CSV.") from exc

    def download_csv(self, key: str, checksum: str, size: int):
        # Ownership transfers to the streaming response after verification.
        target = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")  # noqa: SIM115
        body = None
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            body = response["Body"]
            digest, count = hashlib.sha256(), 0
            for chunk in body.iter_chunks(chunk_size=128 * 1024):
                count += len(chunk)
                if count > size:
                    raise DatasetStorageError(
                        "Stored CSV length does not match its version."
                    )
                digest.update(chunk)
                target.write(chunk)
            if count != size or digest.hexdigest() != checksum:
                raise DatasetStorageError(
                    "Stored CSV checksum does not match its version."
                )
            target.seek(0)
            return target
        except Exception as exc:
            target.close()
            if isinstance(exc, DatasetStorageError):
                raise
            raise DatasetStorageError("Could not download the training CSV.") from exc
        finally:
            if body is not None:
                body.close()

    def delete_staged(self, key: str):
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except (BotoCoreError, ClientError) as exc:
            raise DatasetStorageError("Could not clean up a staged CSV.") from exc

    def close(self):
        self.client.close()
