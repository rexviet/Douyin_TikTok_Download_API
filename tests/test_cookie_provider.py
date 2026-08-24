import asyncio

import pytest


@pytest.fixture(autouse=True)
def _reset_cookie_provider_globals(monkeypatch):
    """Reset cookie_provider module-level state and env vars between tests."""
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


# ---------------------------------------------------------------------------
# Sync entry point (get_cookie / fallback_cookie / resolve_cookie). This is
# what scripts/prove-cookie-swap.py and anything outside an event loop calls.
# get_douyin_headers() itself now uses the async path (see F9 in
# PATCH_NOTES.md / test_cookie_provider_async.py), so it is exercised here
# directly rather than through DouyinWebCrawler.
# ---------------------------------------------------------------------------


def test_sync_get_cookie_from_redis(monkeypatch):
    import fakeredis
    import redis
    import crawlers.douyin.web.cookie_provider as cp

    fake_server = fakeredis.FakeServer()
    fake_client = fakeredis.FakeStrictRedis(server=fake_server, decode_responses=True)

    monkeypatch.setattr(
        redis.Redis, "from_url", classmethod(lambda cls, *a, **kw: fake_client)
    )
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")

    fake_client.set(cp._KEY, "sentinel-cookie-1")
    assert cp.get_cookie() == "sentinel-cookie-1"
    assert cp.resolve_cookie("config-value") == "sentinel-cookie-1"

    # No-restart property: change the key value in the same process and
    # confirm the very next call reflects it without re-importing anything.
    fake_client.set(cp._KEY, "sentinel-cookie-2")
    assert cp.get_cookie() == "sentinel-cookie-2"


def test_sync_resolve_cookie_fallback_when_no_key(monkeypatch):
    import crawlers.douyin.web.cookie_provider as cp

    monkeypatch.setenv("DOUYIN_COOKIE_FALLBACK", "fallback-cookie-value")
    # REDIS_URL intentionally not set -> get_cookie() returns None -> fallback used.

    assert cp.resolve_cookie("config-value") == "fallback-cookie-value"


def test_sync_resolve_cookie_fallback_when_key_deleted(monkeypatch):
    """Real-world case: Redis is up and reachable, but the key is absent
    (e.g. it was deleted/expired). This exercises r.get() -> None on a live
    (fake) client, which is a different code path than REDIS_URL being unset
    entirely."""
    import fakeredis
    import redis
    import crawlers.douyin.web.cookie_provider as cp

    fake_server = fakeredis.FakeServer()
    fake_client = fakeredis.FakeStrictRedis(server=fake_server, decode_responses=True)

    monkeypatch.setattr(
        redis.Redis, "from_url", classmethod(lambda cls, *a, **kw: fake_client)
    )
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("DOUYIN_COOKIE_FALLBACK", "fallback-cookie-value")

    # Key exists, then is removed -- client stays live/reachable throughout.
    fake_client.set(cp._KEY, "temporary-cookie")
    fake_client.delete(cp._KEY)

    assert cp.resolve_cookie("config-value") == "fallback-cookie-value"


# ---------------------------------------------------------------------------
# Async entry point (get_douyin_headers -> resolve_cookie_async). This is
# the request hot-path.
# ---------------------------------------------------------------------------


def test_async_cookie_from_redis(monkeypatch):
    import fakeredis
    import redis.asyncio as redis_asyncio
    import crawlers.douyin.web.cookie_provider as cp
    from crawlers.douyin.web.web_crawler import DouyinWebCrawler

    fake_server = fakeredis.FakeServer()
    fake_client = fakeredis.FakeAsyncRedis(server=fake_server, decode_responses=True)

    monkeypatch.setattr(
        redis_asyncio.Redis, "from_url", classmethod(lambda cls, *a, **kw: fake_client)
    )
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")

    crawler = DouyinWebCrawler()

    async def _run():
        await fake_client.set(cp._KEY, "sentinel-cookie-1")
        kwargs1 = await crawler.get_douyin_headers()

        # No-restart property: change the key value in the same process and
        # confirm the very next call reflects it without re-importing anything.
        await fake_client.set(cp._KEY, "sentinel-cookie-2")
        kwargs2 = await crawler.get_douyin_headers()
        return kwargs1, kwargs2

    kwargs1, kwargs2 = asyncio.run(_run())
    assert kwargs1["headers"]["Cookie"] == "sentinel-cookie-1"
    assert kwargs2["headers"]["Cookie"] == "sentinel-cookie-2"
    assert kwargs2["headers"]["Cookie"] != kwargs1["headers"]["Cookie"]


def test_async_fallback_when_no_key(monkeypatch):
    from crawlers.douyin.web.web_crawler import DouyinWebCrawler

    monkeypatch.setenv("DOUYIN_COOKIE_FALLBACK", "fallback-cookie-value")
    # REDIS_URL intentionally not set -> get_cookie_async() returns None -> fallback used.

    crawler = DouyinWebCrawler()
    kwargs = asyncio.run(crawler.get_douyin_headers())
    assert kwargs["headers"]["Cookie"] == "fallback-cookie-value"


def test_async_fallback_when_key_deleted(monkeypatch):
    """Real-world case: Redis is up and reachable, but the key is absent
    (e.g. it was deleted/expired). This exercises r.get() -> None on a live
    (fake) client, which is a different code path than REDIS_URL being unset
    entirely."""
    import fakeredis
    import redis.asyncio as redis_asyncio
    import crawlers.douyin.web.cookie_provider as cp
    from crawlers.douyin.web.web_crawler import DouyinWebCrawler

    fake_server = fakeredis.FakeServer()
    fake_client = fakeredis.FakeAsyncRedis(server=fake_server, decode_responses=True)

    monkeypatch.setattr(
        redis_asyncio.Redis, "from_url", classmethod(lambda cls, *a, **kw: fake_client)
    )
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("DOUYIN_COOKIE_FALLBACK", "fallback-cookie-value")

    crawler = DouyinWebCrawler()

    async def _run():
        # Key exists, then is removed -- client stays live/reachable throughout.
        await fake_client.set(cp._KEY, "temporary-cookie")
        await fake_client.delete(cp._KEY)
        return await crawler.get_douyin_headers()

    kwargs = asyncio.run(_run())
    assert kwargs["headers"]["Cookie"] == "fallback-cookie-value"


def _assert_no_exc_info_leak(record):
    """F6: a warning that merely mentions the exception class name is safe;
    logging str(e)/traceback (e.g. via logger.exception() or exc_info=True)
    is not. record.getMessage() alone can't see this -- it only renders
    msg % args and is blind to exc_info -- so callers must additionally
    check exc_info/exc_text and use the *formatted* (caplog.text) record."""
    assert record.exc_info is None
    assert record.exc_text is None


def test_no_crash_when_redis_down(monkeypatch, caplog):
    """When Redis raises (e.g. connection refused), the async cookie
    resolution must fall back cleanly AND must never log the raw exception
    str() or a traceback, because redis-py exception messages can embed
    reply content (which may include cookie/session data echoed back by a
    misbehaving proxy or server)."""
    import logging
    import redis.asyncio as redis_asyncio
    from crawlers.douyin.web.web_crawler import DouyinWebCrawler

    secret_in_exception = "LEAKED-FROM-STR-E"

    class _RaisingAsyncClient:
        async def get(self, key):
            raise redis_asyncio.ConnectionError(
                f"connection refused; reply was sessionid={secret_in_exception}"
            )

    monkeypatch.setattr(
        redis_asyncio.Redis, "from_url",
        classmethod(lambda cls, *a, **kw: _RaisingAsyncClient()),
    )
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setenv("DOUYIN_COOKIE_FALLBACK", "fallback-cookie-value")

    crawler = DouyinWebCrawler()
    with caplog.at_level(logging.WARNING):
        kwargs = asyncio.run(crawler.get_douyin_headers())

    assert kwargs["headers"]["Cookie"] == "fallback-cookie-value"

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    # A warning MUST have been emitted -- an assertion that passes because
    # nothing was logged proves nothing about what's *in* the log.
    assert len(warnings) == 1
    record = warnings[0]
    _assert_no_exc_info_leak(record)

    # Use the handler-formatted text (caplog.text), not record.getMessage():
    # getMessage() renders only msg % args and would miss a traceback that
    # a formatter or logger.exception()/exc_info=True would append.
    formatted = caplog.text

    assert "fallback-cookie-value" not in formatted
    assert "Redis unavailable" in formatted
    # The class name is fine to log...
    assert "ConnectionError" in formatted
    # ...but the exception's str() payload (which could carry reply content)
    # must never reach the log, whether via %-args or via a traceback.
    assert secret_in_exception not in formatted


@pytest.mark.parametrize("cookie_source", ["redis", "env_fallback"])
def test_cookie_never_in_logs(monkeypatch, caplog, capsys, cookie_source):
    """The cookie must never leak into any observable output: not the log
    records, and not stdout/stderr either (this is what a stray print()
    statement, or a stray logger.debug("got cookie %s", cookie), anywhere on
    the header-building path would produce). Parameterised over both places
    a cookie can come from: Redis (F15) and the env fallback."""
    import logging
    from crawlers.douyin.web.web_crawler import DouyinWebCrawler

    secret_cookie = "sessionid=super-secret-abc123; passport_csrf_token=xyz789"

    if cookie_source == "redis":
        import fakeredis
        import redis.asyncio as redis_asyncio
        import crawlers.douyin.web.cookie_provider as cp

        fake_server = fakeredis.FakeServer()
        fake_client = fakeredis.FakeAsyncRedis(server=fake_server, decode_responses=True)
        monkeypatch.setattr(
            redis_asyncio.Redis, "from_url",
            classmethod(lambda cls, *a, **kw: fake_client),
        )
        monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")

        async def _seed():
            await fake_client.set(cp._KEY, secret_cookie)

        asyncio.run(_seed())
    else:
        assert cookie_source == "env_fallback"
        monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
        monkeypatch.setenv("DOUYIN_COOKIE_FALLBACK", secret_cookie)

    crawler = DouyinWebCrawler()
    with caplog.at_level(logging.DEBUG):
        kwargs = asyncio.run(crawler.get_douyin_headers())

    assert kwargs["headers"]["Cookie"] == secret_cookie

    for record in caplog.records:
        # getMessage() alone is not enough (see F6): also check for an
        # attached traceback, which getMessage() cannot see.
        _assert_no_exc_info_leak(record)
        formatted = record.getMessage()
        assert secret_cookie not in formatted
        assert "sessionid" not in formatted
        assert "passport_" not in formatted

    # Also check the fully formatted (handler-rendered) log text, which
    # would include any traceback a logger.exception()/exc_info=True call
    # appended -- getMessage() on individual records would miss that.
    full_log_text = caplog.text
    assert secret_cookie not in full_log_text
    assert "sessionid" not in full_log_text
    assert "passport_" not in full_log_text

    captured = capsys.readouterr()
    for stream_name, stream_content in (("stdout", captured.out), ("stderr", captured.err)):
        assert secret_cookie not in stream_content, f"cookie leaked to {stream_name}"
        assert "sessionid" not in stream_content, f"'sessionid' leaked to {stream_name}"
        assert "passport_" not in stream_content, f"'passport_' leaked to {stream_name}"
