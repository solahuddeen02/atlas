# core/storage/factory.py
from __future__ import annotations

import os

from core.storage.base import StorageBackend


def get_storage() -> StorageBackend:
    """
    Return storage backend based on STORAGE_BACKEND env var.
    Defaults to local storage.
    """
    backend = os.getenv("STORAGE_BACKEND", "local")

    if backend == "minio":
        from core.storage.minio import MinIOStorage
        return MinIOStorage()

    from core.storage.local import LocalStorage
    return LocalStorage()
