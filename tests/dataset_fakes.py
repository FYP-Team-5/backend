import hashlib
import io

from app.service.dataset_storage import DatasetStorageError


class MemoryDatasetStorage:
    def __init__(self):
        self.objects = {}
        self.fail = False
        self.after_put = None

    def ensure_bucket(self):
        if self.fail:
            raise DatasetStorageError("Storage offline")

    def put_csv(self, key, source, checksum, size):
        self.ensure_bucket()
        value = source.read()
        assert len(value) == size and hashlib.sha256(value).hexdigest() == checksum
        if key in self.objects:
            assert self.objects[key] == value, "Immutable key overwrite"
        self.objects[key] = value
        if self.after_put:
            callback, self.after_put = self.after_put, None
            callback()

    def download_csv(self, key, checksum, size):
        self.ensure_bucket()
        value = self.objects[key]
        if len(value) != size or hashlib.sha256(value).hexdigest() != checksum:
            raise DatasetStorageError("Checksum mismatch")
        return io.BytesIO(value)

    def delete_staged(self, key):
        self.objects.pop(key, None)

    def close(self):
        pass
