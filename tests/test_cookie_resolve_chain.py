"""F12: an all-empty cookie chain (Redis down/unset, no env fallback, and an
empty config.yaml value -- which is what config.yaml now ships with, see
PATCH_NOTES.md item 4) must not go out the door silently. It must produce a
*value-free* warning so an operator can tell "we never had a cookie" apart
from "Douyin rejected our cookie" (the two have opposite remediations)."""

import asyncio
import logging

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


def test_resolve_cookie_warns_when_chain_fully_empty(caplog):
    import crawlers.douyin.web.cookie_provider as cp

    with caplog.at_level(logging.WARNING):
        result = cp.resolve_cookie("")

    assert result == ""
    empty_warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "resolved cookie is empty" in r.getMessage()
    ]
    assert len(empty_warnings) == 1
    # Value-free: the warning must never contain the (empty) cookie value
    # itself, just the fact that it's empty -- guard against someone later
    # "helpfully" adding the value to the message.
    assert "config_value" not in empty_warnings[0].getMessage()


def test_resolve_cookie_async_warns_when_chain_fully_empty(caplog):
    import crawlers.douyin.web.cookie_provider as cp

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(cp.resolve_cookie_async(""))

    assert result == ""
    empty_warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "resolved cookie is empty" in r.getMessage()
    ]
    assert len(empty_warnings) == 1


def test_resolve_cookie_does_not_warn_when_config_value_present(caplog):
    """No false alarm: if Redis/env are empty but config.yaml still has a
    (non-empty) value, that's the legitimate last link in the chain."""
    import crawlers.douyin.web.cookie_provider as cp

    with caplog.at_level(logging.WARNING):
        result = cp.resolve_cookie("some-config-cookie")

    assert result == "some-config-cookie"
    empty_warnings = [
        r for r in caplog.records
        if "resolved cookie is empty" in r.getMessage()
    ]
    assert empty_warnings == []
