"""Atomic writes for the files the IdP bootstrap leaves under secrets/ (ADR-004 §3).

A file is written to a temporary file in its own directory, given its final mode
before the first byte is written, flushed to disk, and then renamed over the target.
So a crash leaves either the old file or the new one, never a half-written file, and
no state in which a file is readable by more than its final mode allows.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import stat
from pathlib import Path
from typing import Literal

FileState = Literal["created", "updated", "kept"]


def write_atomic(path: Path, content: str, *, mode: int) -> None:
    """Replace (or create) `path` with `content` and `mode`.

    The temporary file starts at 0600 and is moved to `mode` with fchmod before any
    content is written (the umask does not apply to fchmod). Its group is the
    directory's when the directory is setgid, which is how the bootstrap's files reach
    the atlas group.
    """
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            os.fchmod(fh.fileno(), mode)
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    # Make the rename itself durable. Best effort: not every filesystem allows it.
    with contextlib.suppress(OSError):
        dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def read_text(path: Path) -> str | None:
    """The file's text, or None if it does not exist or is not a regular file."""
    try:
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def write_if_different(path: Path, content: str, *, mode: int) -> FileState:
    """Write `content` only if the file is missing, differs, or has another mode."""
    current = read_text(path)
    if current is None:
        write_atomic(path, content, mode=mode)
        return "created"
    if current == content and stat.S_IMODE(path.stat().st_mode) == mode:
        return "kept"
    write_atomic(path, content, mode=mode)
    return "updated"
