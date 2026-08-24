"""Bug 2 / F11 (coordinator finding): the old `_warned` boolean latched for
the entire process lifetime -- after the first-ever Redis failure, every
later outage was invisible in the logs while still costing a per-request
try/except. plan.md Sec.7's green/red "Health bar: cookie" lamp would have
no data after minute one with a permanent latch.

This replaces the latch with time-based suppression (re-warn at most once
per _WARN_SUPPRESS_SECONDS) and exposes a consecutive-failure counter a
future health endpoint can read. The clock is injected via monkeypatching
cookie_provider.time.monotonic -- no real sleep()s."""

import pytest


@pytest.fixture(autouse=True)
def _reset_cookie_provider_globals(monkeypatch):
    import crawlers.douyin.web.cookie_provider as cp

    cp._client = None
    cp._async_client = None
    cp._async_loop = None
    cp._last_warned_at = None
    cp._consecutive_failures = 0
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("DOUYIN_COOKIE_FALLBACK", raising=False)
    yield
    cp._client = None
    cp._async_client = None
    cp._async_loop = None
    cp._last_warned_at = None
    cp._consecutive_failures = 0


class _FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_warning_suppressed_within_window(monkeypatch, caplog):
    import logging
    import crawlers.douyin.web.cookie_provider as cp

    clock = _FakeClock()
    monkeypatch.setattr(cp.time, "monotonic", clock.monotonic)

    with caplog.at_level(logging.WARNING):
        cp._warn_redis_unavailable(ConnectionError("boom"))
        clock.advance(5)  # well inside the suppression window
        cp._warn_redis_unavailable(ConnectionError("boom again"))

    warnings = [r for r in caplog.records if "Redis unavailable" in r.getMessage()]
    assert len(warnings) == 1, "second warning within the window should be suppressed"


def test_warning_reemitted_after_window_elapses(monkeypatch, caplog):
    import logging
    import crawlers.douyin.web.cookie_provider as cp

    clock = _FakeClock()
    monkeypatch.setattr(cp.time, "monotonic", clock.monotonic)

    with caplog.at_level(logging.WARNING):
        cp._warn_redis_unavailable(ConnectionError("boom"))
        clock.advance(cp._WARN_SUPPRESS_SECONDS + 1)  # window has elapsed
        cp._warn_redis_unavailable(ConnectionError("boom again"))

    warnings = [r for r in caplog.records if "Redis unavailable" in r.getMessage()]
    assert len(warnings) == 2, (
        "a fresh outage after the suppression window must produce a new "
        "warning -- a permanent latch would give the health lamp no data "
        "after the first-ever failure"
    )


def test_consecutive_failures_counter_increments_and_resets(monkeypatch):
    import fakeredis
    import redis
    import crawlers.douyin.web.cookie_provider as cp

    class _RaisingClient:
        def get(self, key):
            raise redis.ConnectionError("down")

    monkeypatch.setattr(
        redis.Redis, "from_url", classmethod(lambda cls, *a, **kw: _RaisingClient())
    )
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")

    assert cp._consecutive_failures == 0
    cp.get_cookie()
    assert cp._consecutive_failures == 1
    cp.get_cookie()
    assert cp._consecutive_failures == 2

    # A subsequent success (e.g. Redis recovers) must reset the counter --
    # a future health endpoint reads this to distinguish "currently down"
    # from "had one blip an hour ago".
    fake_server = fakeredis.FakeServer()
    fake_client = fakeredis.FakeStrictRedis(server=fake_server, decode_responses=True)
    monkeypatch.setattr(
        redis.Redis, "from_url", classmethod(lambda cls, *a, **kw: fake_client)
    )
    cp._client = None  # force _redis() to rebuild against the working client
    fake_client.set(cp._KEY, "back-up")

    assert cp.get_cookie() == "back-up"
    assert cp._consecutive_failures == 0
