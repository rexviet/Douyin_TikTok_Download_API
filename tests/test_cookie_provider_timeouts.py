"""F10: socket_timeout=1 / socket_connect_timeout=1 are declared
load-bearing in PATCH_NOTES.md ("without them a hung Redis hangs every
Douyin request") but nothing enforced it -- every other test replaces
redis.Redis.from_url / redis.asyncio.Redis.from_url with a lambda that
discards **kw, so deleting both kwargs from cookie_provider.py would still
pass the whole suite. These tests capture the kwargs actually passed
in and assert on them, for both the sync and async clients."""

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


def test_sync_client_gets_load_bearing_timeouts(monkeypatch):
    import redis
    import crawlers.douyin.web.cookie_provider as cp

    captured_kwargs = {}

    def _fake_from_url(cls, *args, **kwargs):
        captured_kwargs.update(kwargs)
        return object()

    monkeypatch.setattr(redis.Redis, "from_url", classmethod(_fake_from_url))
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")

    cp._redis()

    assert captured_kwargs.get("socket_timeout") == 1
    assert captured_kwargs.get("socket_connect_timeout") == 1


def test_async_client_gets_load_bearing_timeouts(monkeypatch):
    import redis.asyncio as redis_asyncio
    import crawlers.douyin.web.cookie_provider as cp

    captured_kwargs = {}

    def _fake_from_url(cls, *args, **kwargs):
        captured_kwargs.update(kwargs)
        return object()

    monkeypatch.setattr(redis_asyncio.Redis, "from_url", classmethod(_fake_from_url))
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")

    cp._redis_async()

    assert captured_kwargs.get("socket_timeout") == 1
    assert captured_kwargs.get("socket_connect_timeout") == 1
