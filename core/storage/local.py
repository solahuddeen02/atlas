# core/storage/local.py
from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO, Tuple

from core.storage.base import StorageBackend

CHUNK_SIZE = 1024 * 1024  # 1MB


class LocalStorage(StorageBackend):

    def __init__(self, data_dir: str | None = None):
        self.data_dir = Path(data_dir or os.getenv("ATLAS_DATA_DIR", "data"))

    def save(self, object_id: int, src: BinaryIO) -> Tuple[str, int]:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        final_path = self.data_dir / str(object_id)
        tmp_path = self.data_dir / f".{object_id}.tmp"
        size = 0

        try:
            with open(tmp_path, "wb") as f:
                while True:
                    chunk = src.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    f.write(chunk)
                    size += len(chunk)
            os.replace(tmp_path, final_path)
            return str(final_path), size
        except Exception:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass
            raise

    def delete(self, storage_key: str) -> None:
        path = Path(storage_key)
        if path.exists():
            path.unlink()

    def exists(self, storage_key: str) -> bool:
        return Path(storage_key).exists()

    def get_path(self, storage_key: str) -> str:
        return storage_key


# backward-compatible function สำหรับ code เดิมที่ยังใช้อยู่
def save_file(object_id: int, src: BinaryIO) -> Tuple[str, int]:
    return LocalStorage().save(object_id, src)
