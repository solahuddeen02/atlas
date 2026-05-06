from __future__ import annotations

import io
from typing import BinaryIO

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
