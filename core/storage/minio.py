# core/storage/minio.py
from __future__ import annotations

import os
from typing import BinaryIO, Tuple

from core.storage.base import StorageBackend


class MinIOStorage(StorageBackend):

    def __init__(
        self,
        endpoint: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        bucket: str | None = None,
        secure: bool = False,
    ):
        try:
            from minio import Minio
        except ImportError:
            raise RuntimeError(
                "minio package not installed. "
                "Add 'minio>=7.0.0' to requirements.txt"
            )

        self.bucket = bucket or os.getenv("MINIO_BUCKET", "atlas")
        self.client = Minio(
            endpoint or os.getenv("MINIO_ENDPOINT", "localhost:9000"),
            access_key=access_key or os.getenv("MINIO_ACCESS_KEY", "minioadmin"),
            secret_key=secret_key or os.getenv("MINIO_SECRET_KEY", "minioadmin"),
            secure=secure,
        )
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        if not self.client.bucket_exists(self.bucket):
            self.client.make_bucket(self.bucket)

    def save(self, object_id: int, src: BinaryIO) -> Tuple[str, int]:
        # อ่านข้อมูลก่อนเพื่อรู้ขนาด
        data = src.read()
        size = len(data)

        import io
        storage_key = f"objects/{object_id}"
        self.client.put_object(
            self.bucket,
            storage_key,
            io.BytesIO(data),
            length=size,
        )
        return storage_key, size

    def delete(self, storage_key: str) -> None:
        self.client.remove_object(self.bucket, storage_key)

    def exists(self, storage_key: str) -> bool:
        try:
            self.client.stat_object(self.bucket, storage_key)
            return True
        except Exception:
            return False

    def get_path(self, storage_key: str) -> str:
        from datetime import timedelta
        return self.client.presigned_get_object(
            self.bucket,
            storage_key,
            expires=timedelta(hours=1),
        )

    def save_thumb(self, obj_id: int, data: bytes) -> None:
        import io
        self.client.put_object(
            self.bucket, f"thumbs/{obj_id}.jpg",
            io.BytesIO(data), length=len(data), content_type="image/jpeg",
        )

    def thumb_exists(self, obj_id: int) -> bool:
        try:
            self.client.stat_object(self.bucket, f"thumbs/{obj_id}.jpg")
            return True
        except Exception:
            return False

    def get_thumb_path(self, obj_id: int) -> str:
        from datetime import timedelta
        return self.client.presigned_get_object(
            self.bucket, f"thumbs/{obj_id}.jpg", expires=timedelta(hours=1),
        )
