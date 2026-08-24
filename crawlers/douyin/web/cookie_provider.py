import asyncio
import logging, os, time
logger = logging.getLogger(__name__)
_KEY = "douyin:cookie:current"
_client = None
_async_client = None
# The running loop _async_client was built on. redis.asyncio clients are
# bound to the loop that created them; reusing one across asyncio.run()
# calls (uvicorn --reload, a worker that makes a fresh loop per task, two
# sequential asyncio.run()s) raises "got Future attached to a different
# loop", which get_cookie_async() would otherwise swallow and silently,
# permanently fall back to empty.
_async_loop = None

# Time-based warning suppression (F11), not a one-shot latch: a permanent
# latch means every outage after the first-ever one is invisible in the
# logs while still costing a per-request try/except, and the plan.md Sec.7
# "Health bar: cookie" lamp would have no data after minute one.
_WARN_SUPPRESS_SECONDS = 60
_last_warned_at = None
# Consecutive Redis failures since the last success. A future health
# endpoint can read this directly instead of parsing logs.
_consecutive_failures = 0

# Load-bearing, not tuning: without these a hung/unreachable Redis hangs
# every Douyin request (sync path) or stalls the event loop (async path).
_REDIS_KW = {"socket_timeout": 1, "socket_connect_timeout": 1}

def _redis():
    global _client
    if _client is None:
        url = os.environ.get("REDIS_URL")
        if not url:
            return None
        import redis
        _client = redis.Redis.from_url(url, decode_responses=True, **_REDIS_KW)
    return _client

def _redis_async():
    global _async_client, _async_loop
    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None
    if _async_client is None or _async_loop is not running_loop:
        url = os.environ.get("REDIS_URL")
        if not url:
            return None
        import redis.asyncio as redis_asyncio
        _async_client = redis_asyncio.Redis.from_url(url, decode_responses=True, **_REDIS_KW)
        _async_loop = running_loop
    return _async_client

def _warn_redis_unavailable(e):
    # NEVER log the cookie value, and never log str(e) either —
    # redis-py exception messages can contain reply content.
    global _last_warned_at, _consecutive_failures
    _consecutive_failures += 1
    now = time.monotonic()
    if _last_warned_at is None or (now - _last_warned_at) >= _WARN_SUPPRESS_SECONDS:
        logger.warning("cookie_provider: Redis unavailable (%s), using fallback",
                       type(e).__name__)          # class name ONLY, never str(e)
        _last_warned_at = now

def _note_redis_success():
    global _consecutive_failures
    _consecutive_failures = 0

def get_cookie():
    # Sync entry point (used by scripts/prove-cookie-swap.py). Returns
    # cookie from Redis, or None so caller uses fallback.
    try:
        r = _redis()
        if not r:
            return None
        value = r.get(_KEY)
        _note_redis_success()
        return value
    except Exception as e:
        _warn_redis_unavailable(e)
        return None

async def get_cookie_async():
    # Async entry point used on the request hot-path (see
    # resolve_cookie_async / get_douyin_headers). Must be awaited so an
    # unreachable Redis never blocks the event loop -- see F9 in
    # PATCH_NOTES.md.
    try:
        r = _redis_async()
        if not r:
            return None
        value = await r.get(_KEY)
        _note_redis_success()
        return value
    except Exception as e:
        _warn_redis_unavailable(e)
        return None

def fallback_cookie():
    return os.environ.get("DOUYIN_COOKIE_FALLBACK", "")

def _warn_if_empty(cookie):
    if not cookie:
        # Value-free: never include the (empty) cookie itself, just the fact.
        logger.warning(
            "cookie_provider: resolved cookie is empty (Redis, "
            "DOUYIN_COOKIE_FALLBACK, and config.yaml are all empty)"
        )
    return cookie

def resolve_cookie(config_value):
    """Sync cookie chain: Redis -> env fallback -> config value. Used by
    scripts/prove-cookie-swap.py and anywhere outside an event loop."""
    return _warn_if_empty(get_cookie() or fallback_cookie() or config_value)

async def resolve_cookie_async(config_value):
    """Async cookie chain, identical precedence to resolve_cookie() but
    non-blocking. This is what get_douyin_headers() must call."""
    return _warn_if_empty(await get_cookie_async() or fallback_cookie() or config_value)
