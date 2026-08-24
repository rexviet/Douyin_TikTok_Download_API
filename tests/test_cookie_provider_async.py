"""F9: get_douyin_headers() is async but must never block the event loop on
a slow/unreachable Redis. These tests exercise the async cookie-resolution
path directly against redis.asyncio, independent of test_cookie_provider.py
(which covers the sync path used by scripts/prove-cookie-swap.py)."""

import asyncio
import time

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


def test_concurrent_headers_not_serialized_by_slow_redis(monkeypatch):
    """Simulate a Redis whose GET takes SLEEP seconds and fire N concurrent
    get_douyin_headers() calls. A blocking (sync-call-from-async) client
    serializes them: total wall time ~= N * SLEEP. A correctly awaited
    async client overlaps them: total wall time stays close to one SLEEP."""
    import redis.asyncio as redis_asyncio
    from crawlers.douyin.web.web_crawler import DouyinWebCrawler

    SLEEP = 0.2
    N = 5

    class _SlowAsyncClient:
        async def get(self, key):
            await asyncio.sleep(SLEEP)
            return "sentinel-async-cookie"

    monkeypatch.setattr(
        redis_asyncio.Redis, "from_url",
        classmethod(lambda cls, *a, **kw: _SlowAsyncClient()),
    )
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")

    crawler = DouyinWebCrawler()

    async def _run():
        start = time.perf_counter()
        results = await asyncio.gather(
            *[crawler.get_douyin_headers() for _ in range(N)]
        )
        elapsed = time.perf_counter() - start
        return results, elapsed

    results, elapsed = asyncio.run(_run())

    assert all(r["headers"]["Cookie"] == "sentinel-async-cookie" for r in results)
    # Serialized would take N * SLEEP (~1.0s); non-blocking stays near SLEEP.
    assert elapsed < SLEEP * (N / 2), (
        f"elapsed={elapsed:.3f}s looks serialized (N*SLEEP={N * SLEEP:.3f}s); "
        "the Redis call is blocking the event loop instead of being awaited"
    )
