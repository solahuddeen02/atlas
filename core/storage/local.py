# core/storage/local.py
from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO, Tuple

CHUNK_SIZE = 1024 * 1024  # 1MB
DATA_DIR = os.getenv("ATLAS_DATA_DIR", "data")


def save_file(obj_id: int, src: BinaryIO) -> Tuple[str, int]:
    """
    Atomic-ish local save:
    - write to temp file
    - os.replace to final path (atomic on same filesystem)
    Returns (final_path, size_bytes)
    """
    base = Path(DATA_DIR)
    base.mkdir(parents=True, exist_ok=True)

    final_path = base / f"{obj_id}"
    tmp_path = base / f".{obj_id}.tmp"

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
        # Cleanup temp file on failure
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass
        raise
