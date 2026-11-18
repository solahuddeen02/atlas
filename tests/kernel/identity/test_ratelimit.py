"""App-layer rate limits: fixed windows in Postgres (ADR-003 §5), atomic under concurrency."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import Engine, text

from atlas.kernel.identity.ratelimit import MAX_KEY_LENGTH, RateLimiter, RateSpec
from tests.kernel.conftest import FrozenClock


@pytest.mark.parametrize(
    ("text_", "count", "seconds"),
    [("20r/m", 20, 60), ("5r/s", 5, 1), ("1r/m", 1, 60), ("999999r/s", 999999, 1)],
)
def test_a_rate_is_parsed(text_: str, count: int, seconds: int) -> None:
    assert RateSpec.parse(text_) == RateSpec(count, seconds)


@pytest.mark.parametrize(
    "bad", ["", "20", "20r", "r/m", "0r/m", "-1r/m", "20r/h", "20 r/m", "20R/M"]
)
def test_a_bad_rate_is_refused(bad: str) -> None:
    with pytest.raises(ValueError, match="a rate looks like"):
        RateSpec.parse(bad)


def test_the_limit_is_exact(app_engine: Engine, clock: FrozenClock) -> None:
    limiter = RateLimiter(app_engine, clock=clock)
    spec = RateSpec(3, 60)
    decisions = [limiter.hit("auth:login:ip:192.0.2.1", spec) for _ in range(5)]
    assert [d.allowed for d in decisions] == [True, True, True, False, False]
    assert [d.count for d in decisions] == [1, 2, 3, 4, 5]


def test_retry_after_is_the_rest_of_the_window(app_engine: Engine, clock: FrozenClock) -> None:
    limiter = RateLimiter(app_engine, clock=clock)
    spec = RateSpec(1, 60)
    # Start of a window: 12:00:00 is a whole minute.
    assert limiter.hit("k", spec).retry_after == 60
    clock.advance(seconds=45)
    assert limiter.hit("k", spec).retry_after == 15
    clock.advance(seconds=14, milliseconds=500)
    assert limiter.hit("k", spec).retry_after == 1  # never below one second


def test_a_new_window_starts_again(app_engine: Engine, clock: FrozenClock) -> None:
    limiter = RateLimiter(app_engine, clock=clock)
    spec = RateSpec(2, 60)
    assert [limiter.hit("k", spec).allowed for _ in range(3)] == [True, True, False]
    clock.advance(seconds=60)
    assert limiter.hit("k", spec).allowed is True


def test_keys_do_not_share_a_count(app_engine: Engine, clock: FrozenClock) -> None:
    limiter = RateLimiter(app_engine, clock=clock)
    spec = RateSpec(1, 60)
    assert limiter.hit("auth:login:ip:192.0.2.1", spec).allowed
    assert limiter.hit("auth:login:ip:192.0.2.2", spec).allowed
    assert limiter.hit("auth:callback:ip:192.0.2.1", spec).allowed
    assert not limiter.hit("auth:login:ip:192.0.2.1", spec).allowed


@pytest.mark.parametrize(
    "key", ["", "x" * (MAX_KEY_LENGTH + 1), "has\nnewline", "nul\x00", "del\x7f"]
)
def test_a_bad_key_is_refused(app_engine: Engine, key: str) -> None:
    with pytest.raises(ValueError, match="1 to 200 printable"):
        RateLimiter(app_engine).hit(key, RateSpec(1, 60))


def test_the_counter_is_atomic_under_parallel_requests(
    app_engine: Engine, clock: FrozenClock
) -> None:
    """40 requests at once against a limit of 10: exactly 10 are allowed, none is lost."""
    limiter = RateLimiter(app_engine, clock=clock)  # a frozen clock: one window, always
    spec = RateSpec(10, 3600)
    workers = 40
    barrier = threading.Barrier(workers)

    def one(_: int) -> tuple[bool, int]:
        barrier.wait()
        decision = limiter.hit("auth:callback:ip:192.0.2.99", spec)
        return decision.allowed, decision.count

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(one, range(workers)))

    assert sum(allowed for allowed, _ in results) == 10
    # Every request got its own number: 1..40, none twice, none missing.
    assert sorted(count for _, count in results) == list(range(1, workers + 1))
    with app_engine.connect() as connection:
        stored = connection.execute(
            text("SELECT count FROM kernel.rate_limits WHERE key = 'auth:callback:ip:192.0.2.99'")
        ).scalar_one()
    assert stored == workers


def test_old_windows_are_removed(app_engine: Engine, clock: FrozenClock) -> None:
    limiter = RateLimiter(app_engine, clock=clock)
    limiter.hit("old", RateSpec(5, 60))
    clock.advance(hours=2)
    limiter.hit("new", RateSpec(5, 60))
    with app_engine.connect() as connection:
        keys = {r[0] for r in connection.execute(text("SELECT key FROM kernel.rate_limits"))}
    assert keys == {"new"}


def test_the_purge_runs_at_most_once_a_minute(app_engine: Engine, clock: FrozenClock) -> None:
    limiter = RateLimiter(app_engine, clock=clock)
    limiter.hit("first", RateSpec(5, 60))  # purges (nothing yet) and starts the interval
    clock.advance(hours=2)
    limiter.hit("second", RateSpec(5, 60))  # purges: "first" is two hours old
    clock.advance(seconds=30)
    limiter.hit("third", RateSpec(5, 60))
    with app_engine.connect() as connection:
        keys = {r[0] for r in connection.execute(text("SELECT key FROM kernel.rate_limits"))}
    assert keys == {"second", "third"}
