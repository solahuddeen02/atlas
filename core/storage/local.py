# core/storage/local.py
from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO, Tuple

CHUNK_SIZE = 1024 * 1024  # 1MB

# Default folder for stored binaries (gitignore)
DATA_DIR = os.getenv("ATLAS_DATA_DIR", "data")


def save_file(obj_id: int, src: BinaryIO) -> Tuple[str, int]:
    """
    Save a binary stream to local disk and return (path, size_bytes).
    Designed to work with UploadFile.file (a file-like object).
    """
    base = Path(DATA_DIR)
    base.mkdir(parents=True, exist_ok=True)

    # Keep it simple: one object = one file path
    # You can later add subfolders by obj_id ranges if needed.
    dst_path = base / f"{obj_id}"

    size = 0
    with open(dst_path, "wb") as f:
        while True:
            chunk = src.read(CHUNK_SIZE)
            if not chunk:
                break
            f.write(chunk)
            size += len(chunk)

    return str(dst_path), size
