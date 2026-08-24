"""Bug 1 (coordinator finding): redis.asyncio clients are bound to the
event loop they were created on. cookie_provider caches _async_client at
module level; if two separate asyncio.run() calls (uvicorn --reload, a
worker that makes a fresh loop per task, or just two sequential
asyncio.run()s as in scripts/prove-cookie-swap.py) each try to reuse that
cached client, the second call's `await client.get(...)` raises
`RuntimeError: got Future attached to a different loop`. That gets
swallowed by get_cookie_async()'s `except Exception`, and the cookie
silently, permanently falls back to the empty config value for the rest of
the process (and _warned latching meant only one warning was ever emitted --
see test_cookie_provider_warn_suppression.py for that half of the fix).

This test deliberately does NOT reset cp._async_client between the two
asyncio.run() calls in its fixture -- resetting it would hide exactly the
bug under test, which is why this lives in its own file with its own
fixture rather than reusing the autouse fixture in test_cookie_provider.py.
"""

import asyncio

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


def test_get_cookie_async_rebuilds_client_across_event_loops(monkeypatch):
    import redis.asyncio as redis_asyncio
    import crawlers.douyin.web.cookie_provider as cp

    class _LoopBoundClient:
        def __init__(self):
            self._loop = asyncio.get_event_loop()

        async def get(self, key):
            if asyncio.get_running_loop() is not self._loop:
                raise RuntimeError("got Future attached to a different loop")
            return "value-from-redis"

    monkeypatch.setattr(
        redis_asyncio.Redis, "from_url",
        classmethod(lambda cls, *a, **kw: _LoopBoundClient()),
    )
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")

    # Two independent event loops, exactly like two sequential
    # asyncio.run() calls in a real process (uvicorn --reload / a worker
    # that spins up a fresh loop per task / prove-cookie-swap.py).
    result1 = asyncio.run(cp.get_cookie_async())
    result2 = asyncio.run(cp.get_cookie_async())

    assert result1 == "value-from-redis"
    assert result2 == "value-from-redis", (
        "second call silently fell back (likely None) -- the cached async "
        "client from loop 1 was reused on loop 2 and raised RuntimeError, "
        "which get_cookie_async() swallowed"
    )
