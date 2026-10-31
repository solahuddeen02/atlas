"""UUIDv7 (RFC 9562): time-ordered, unguessable ids for anything exposed in URLs.

Python 3.11 has no `uuid.uuid7` and PostgreSQL 17 has no `uuidv7()`, so ids are
generated here.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

_lock = threading.Lock()
_last_ms = 0
_last_counter = 0


def uuid7() -> uuid.UUID:
    """A UUIDv7. Monotonic within a process: ids from the same millisecond still sort."""
    global _last_ms, _last_counter
    with _lock:
        ms = time.time_ns() // 1_000_000
        if ms > _last_ms:
            _last_ms = ms
            _last_counter = int.from_bytes(os.urandom(2), "big") & 0x7FF  # leave headroom
        else:
            # Same (or earlier) millisecond: keep the timestamp, bump the 12-bit counter.
            _last_counter += 1
            if _last_counter > 0xFFF:
                _last_ms += 1
                _last_counter = 0
        ms, counter = _last_ms, _last_counter

    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76  # version 7
    value |= counter << 64  # rand_a: 12 bits, used as a counter (RFC 9562 §6.2, method 1)
    value |= 0b10 << 62  # variant
    value |= rand_b
    return uuid.UUID(int=value)
