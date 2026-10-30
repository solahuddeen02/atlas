# core/upload_limit.py
from __future__ import annotations

import json

from core.security import get_max_upload_bytes

# Allowance for multipart boundaries and part headers on top of the file itself.
MULTIPART_OVERHEAD_BYTES = 64 * 1024


async def _send_413(send) -> None:
    body = json.dumps({"detail": "upload too large"}).encode()
    await send({
        "type": "http.response.start",
        "status": 413,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"connection", b"close"),
        ],
    })
    await send({"type": "http.response.body", "body": body})


class UploadSizeLimitMiddleware:
    """
    Reject oversized upload requests before the multipart body is spooled to disk.

    Checks Content-Length up front, then counts bytes as they stream in, so a
    missing or lying Content-Length cannot bypass the limit. The storage layer
    enforces the exact per-file limit on top of this.
    """

    def __init__(self, app, path: str = "/objects/upload"):
        self.app = app
        self.path = path

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] != self.path:
            await self.app(scope, receive, send)
            return

        limit = get_max_upload_bytes() + MULTIPART_OVERHEAD_BYTES

        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    if int(value) > limit:
                        await _send_413(send)
                        return
                except ValueError:
                    pass

        received = 0
        too_large = False
        response_started = False

        async def limited_receive():
            nonlocal received, too_large
            if too_large:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    # Stop feeding the body. The framework may turn the cut-off body into
                    # its own error (e.g. 400); limited_send replaces that with a 413.
                    too_large = True
                    return {"type": "http.disconnect"}
            return message

        async def limited_send(message):
            nonlocal response_started
            if too_large:
                if message["type"] == "http.response.start" and not response_started:
                    response_started = True
                    await _send_413(send)
                return  # drop the app's own response
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, limited_send)
        except Exception:
            if not too_large:
                raise
        if too_large and not response_started:
            await _send_413(send)
