"""App-layer rate limits (ADR-003 §5, item 3): fixed-window counters in Postgres.

One statement does the counting, `INSERT ... ON CONFLICT DO UPDATE ... RETURNING`, so
parallel requests cannot lose an increment: Postgres serialises them on the row. A hit that
is over the limit still counts, which is what makes a flood stay blocked.

The key names what is limited (`auth:login:ip:<address>`). The address in a key is the
client address of ADR-003 §4: used for limits and the audit log only, never to decide what
someone may do.
"""

from __future__ import annotations

import math
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, delete
from sqlalchemy.dialects.postgresql import insert

from atlas.kernel.db.metadata import rate_limits

_RATE = re.compile(r"^([1-9][0-9]{0,5})r/([sm])$")
_WINDOW_SECONDS = {"s": 1, "m": 60}
MAX_KEY_LENGTH = 200
# Windows are at most a minute long: anything an hour old is dead weight.
PURGE_AFTER = timedelta(hours=1)
PURGE_EVERY = timedelta(minutes=1)


@dataclass(frozen=True)
class RateSpec:
    """`count` requests per `window_seconds`, written like the proxy's `20r/m`."""

    count: int
    window_seconds: int

    @classmethod
    def parse(cls, text: str) -> RateSpec:
        match = _RATE.match(text)
        if match is None:
            raise ValueError("a rate looks like 20r/m or 5r/s")
        return cls(count=int(match.group(1)), window_seconds=_WINDOW_SECONDS[match.group(2)])


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    # Seconds until the window ends (at least 1): the `Retry-After` of a 429.
    retry_after: int
    count: int


def _utc_now() -> datetime:
    return datetime.now(UTC)


class RateLimiter:
    def __init__(self, engine: Engine, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self._engine = engine
        self._clock = clock
        self._last_purge: datetime | None = None
        self._purge_lock = threading.Lock()

    def hit(self, key: str, spec: RateSpec) -> RateDecision:
        """Count one request under `key` and say whether it is within `spec`."""
        if not key or len(key) > MAX_KEY_LENGTH or any(ord(c) < 32 or ord(c) == 127 for c in key):
            raise ValueError("a rate-limit key is 1 to 200 printable characters")
        now = self._clock().astimezone(UTC)
        epoch = int(now.timestamp())
        start = epoch - epoch % spec.window_seconds
        statement = (
            insert(rate_limits)
            .values(key=key, window_start=datetime.fromtimestamp(start, UTC), count=1)
            .on_conflict_do_update(
                index_elements=[rate_limits.c.key, rate_limits.c.window_start],
                set_={"count": rate_limits.c.count + 1},
            )
            .returning(rate_limits.c.count)
        )
        with self._engine.begin() as connection:
            count = int(connection.execute(statement).scalar_one())
        self._maybe_purge(now)
        retry_after = max(1, math.ceil(start + spec.window_seconds - now.timestamp()))
        return RateDecision(allowed=count <= spec.count, retry_after=retry_after, count=count)

    def _maybe_purge(self, now: datetime) -> None:
        with self._purge_lock:
            if self._last_purge is not None and now - self._last_purge < PURGE_EVERY:
                return
            self._last_purge = now
        with self._engine.begin() as connection:
            connection.execute(
                delete(rate_limits).where(rate_limits.c.window_start < now - PURGE_AFTER)
            )
