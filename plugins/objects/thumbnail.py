from __future__ import annotations

import io
from typing import Any, BinaryIO

THUMB_SIZE = (400, 400)


def make_thumbnail(src: str | BinaryIO) -> bytes | None:
    try:
        from PIL import Image
        with Image.open(src) as img:
            img.thumbnail(THUMB_SIZE)
            if img.mode in ("RGBA", "P", "LA"):
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            return buf.getvalue()
    except Exception:
        return None


def on_object_uploaded(payload: dict[str, Any]) -> None:
    file_data: bytes | None = payload.get("file_data")
    if not file_data:
        return
    from core.storage.factory import get_storage
    thumb = make_thumbnail(io.BytesIO(file_data))
    if thumb:
        get_storage().save_thumb(payload["id"], thumb)
