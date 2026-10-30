# core/storage/base.py
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import BinaryIO, Tuple


class UploadTooLarge(Exception):
    """Raised by save() when the source exceeds max_bytes. Nothing is left in storage."""


class StorageBackend(ABC):

    @abstractmethod
    def save(self, object_id: int, src: BinaryIO, max_bytes: int | None = None) -> Tuple[str, int]:
        """
        Save file and return (storage_key, size_bytes).
        Raise UploadTooLarge if more than max_bytes are read.
        """
        ...

    @abstractmethod
    def delete(self, storage_key: str) -> None:
        """
        Delete file by storage key
        """
        ...

    @abstractmethod
    def exists(self, storage_key: str) -> bool:
        """
        Check if file exists
        """
        ...

    @abstractmethod
    def get_path(self, storage_key: str) -> str:
        """
        Return path or URL for serving the file
        """
        ...
