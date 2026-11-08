# PATCH_NOTES

Local patch carried on top of upstream
[`Evil0ctal/Douyin_TikTok_Download_API`](https://github.com/Evil0ctal/Douyin_TikTok_Download_API).

**Purpose of the whole patch, in one sentence:** the Douyin cookie must be
swappable at runtime, from Redis, without restarting the process or blocking
the event loop — and must never be written to a tracked file or to a log.

**Run this checklist on every `git pull upstream main`.** Verify with
`../../scripts/check-patch-size.sh` from the outer repo, which gates items 4 and 5
automatically. Items 1–3 still need eyes.

---

## 1. New file: `crawlers/douyin/web/cookie_provider.py`

Ours entirely; upstream has no version of it, so it can never conflict. Reads
`douyin:cookie:current` from Redis (`$REDIS_URL`), falls back to
`$DOUYIN_COOKIE_FALLBACK`, then to `config.yaml`.

Public functions, both sync and async (the async ones exist so the request
hot-path never blocks the event loop on a hung/unreachable Redis — see item 2):

- `get_cookie()` / `get_cookie_async()` — read the Redis key directly, `None` on any failure.
- `fallback_cookie()` — reads `$DOUYIN_COOKIE_FALLBACK`.
- `resolve_cookie(config_value)` / `resolve_cookie_async(config_value)` — the
  full chain (Redis → env fallback → `config_value`), plus a **value-free**
  warning if the whole chain resolves empty (an empty `Cookie` header is
  otherwise silent and looks identical to "Douyin rejected our cookie", which
  has the opposite remediation). `scripts/prove-cookie-swap.py` in the outer
  repo calls the sync functions directly.

`socket_timeout=1` and `socket_connect_timeout=1` are **load-bearing, not
tuning**, on *both* the sync and async Redis clients: without them a hung
Redis hangs every Douyin request (sync) or stalls the whole asyncio event
loop (async, including the parser's own health endpoint). Do not remove
them. `tests/test_cookie_provider_timeouts.py` captures the kwargs actually
passed to `from_url` for both clients and fails if either timeout is
dropped, so this is enforced by CI now, not just by this comment.

The warning path never logs `str(e)` of a Redis exception (only the
exception's class name) and never logs the cookie value itself.
`tests/test_cookie_provider.py` asserts this using `caplog.text` (not
`record.getMessage()`, which is blind to `exc_info`/tracebacks) and asserts
`record.exc_info is None`, so switching the warning to `logger.exception(...)`
or adding `exc_info=True` fails CI.

**The async Redis client is keyed to the event loop it was built on, not
cached as a single module-level singleton.** `redis.asyncio` clients are
bound to the loop that created them; a plain module-level cache breaks the
moment a process makes a second loop (`uvicorn --reload`, a worker that
creates a fresh loop per task, or just two sequential `asyncio.run()`
calls — this is exactly how `scripts/prove-cookie-swap.py` runs). The
symptom was silent and permanent: the stale client's `.get()` raised
`RuntimeError: got Future attached to a different loop`, `get_cookie_async()`
swallowed it, and the header fell back to the empty `config.yaml` value for
the rest of the process. `_redis_async()` now compares
`asyncio.get_running_loop()` against a stored `_async_loop` and rebuilds the
client on mismatch. `tests/test_cookie_provider_loop_safety.py` reproduces
two independent event loops without resetting the cached client between
them (an autouse fixture that reset it would hide the bug), and fails if the
per-loop check is reverted to a plain `if _async_client is None` cache.

**The "Redis unavailable" warning is time-suppressed, not a one-shot
latch.** A `_warned = True` boolean that never resets means only the
*first-ever* outage in a process is visible in logs; every later outage
(and there will be later outages) is silent while still costing a
try/except per request. `_warn_redis_unavailable()` now re-warns at most
once every `_WARN_SUPPRESS_SECONDS` (60s), and `_consecutive_failures`
counts failures since the last success (reset to 0 on success) for a future
health endpoint to read directly instead of scraping logs.
`tests/test_cookie_provider_warn_suppression.py` injects a fake clock via
`cookie_provider.time.monotonic` (no real `sleep()`s) and asserts both that
a second failure inside the window is suppressed and that one after the
window elapses is not.

## 2. One line in `get_douyin_headers()` — the source of the `Cookie` header

```python
"Cookie": await resolve_cookie_async(douyin_config["headers"]["Cookie"]),
```

Upstream reads `douyin_config["headers"]["Cookie"]` directly. If a pull
reverts this line, the parser silently goes back to the (now empty) config
value and every request starts failing auth. Nothing crashes — so watch for a
spike in blocked jobs, not for an exception.

It must stay `resolve_cookie_async` (awaited), not the sync `get_cookie()` /
`resolve_cookie()`: `get_douyin_headers()` is `async def` and is called at
the top of every fetch method, so a sync Redis call here blocks the entire
event loop during a Redis outage — starving every in-flight Douyin request
and the parser's own health endpoint at once.
`tests/test_cookie_provider_async.py` fires several concurrent
`get_douyin_headers()` calls against a slow fake Redis and fails if they come
back serialized instead of overlapping, so a regression back to a blocking
call fails CI.

The surrounding `get_douyin_headers()` is called at the top of every fetch method
and re-reads the `config` dict each time, which is *why* no call-flow change was
needed. If upstream ever hoists these headers into a module-level constant
computed once at import, this patch stops working and the cookie freezes at
process start. **That is the failure mode to look for when upstream refactors.**

## 3. `update_cookie()` deleted outright — do not let it back

Upstream's `update_cookie()` wrote a caller-supplied cookie straight into the
tracked `config.yaml` via `yaml.dump()`, printing the cookie to **stdout**
three times along the way (before update, the new value, after update). Both
problems are gone because the **entire method is deleted**, not stubbed and
not de-printed — there is nothing left in `web_crawler.py` for
`app/api/endpoints/hybrid_parsing.py`'s `POST /api/hybrid/update_cookie` to
call.

**This is deliberate, and the upstream endpoint is consequently
non-functional.** It looks like a shortcut. It is not — it wrote a live
session credential to disk and printed it to logs, and it is push-model,
whereas we need pull-model (Redis) so the cookie can change without anyone
calling anyone. We never call this endpoint ourselves, so breaking it costs
nothing. `app/api/endpoints/hybrid_parsing.py` itself is intentionally left
alone (deleting the route would cost upstream-owned lines against the patch
budget for no safety benefit — the method it calls no longer exists either
way).

`tests/test_web_crawler_security.py` asserts
`DouyinWebCrawler` has no `update_cookie` attribute at all, so a merge that
brings the method back (with or without the `print()` calls) fails CI.
`tests/test_cookie_provider.py` separately asserts the cookie reaches neither
the logging subsystem nor stdout/stderr on the real (Redis-driven) header
path, so a *new* leak introduced elsewhere on that path also fails CI.

## 4. `config.yaml` → `Cookie: ""`, fallback moved to env

`crawlers/douyin/web/config.yaml` must keep an **empty** `Cookie`. The value that
was there belonged to the upstream author (commit `3b491db`, 2025-03-16), was
public in the upstream repo, and is long dead — nothing of ours was ever exposed.

The point of keeping it empty is forward-looking: **a tracked file with no room
for a cookie is a file our cookie can never be committed into.** Cookies live in
Redis, or in `$DOUYIN_COOKIE_FALLBACK`. Never here.

A pull that restores upstream's cookie value fails both the secret scan and
`check-patch-size.sh`.

Because the config value can end up empty with Redis down and no env
fallback set, `resolve_cookie()`/`resolve_cookie_async()` (item 1) warn
(value-free) when the whole chain is empty, so this state is at least
visible in logs instead of silently sending an empty `Cookie` header.

## 5. ⚠️ `utils.py` keeps its own copy of the config — check this every pull

`web_crawler.py` and `utils.py` each call `yaml.safe_load` separately, so two
independent config dicts exist in memory. Harmless today, because `TokenManager`
only ever reads `msToken`, `ttwid`, and `proxies`, and `gen_ttwid()` POSTs to
Douyin for a fresh ttwid rather than reading `headers.Cookie`. This is the reason
the patch is small (well under the 50-line budget) instead of a rewrite.

If upstream ever makes `TokenManager` read `headers.Cookie`, **the patch springs a
silent leak**: no error, no crash — requests simply start using the stale
(now empty) config cookie while Redis holds the good one. Check on every pull:

```bash
grep -nE 'headers\["Cookie"\]|headers\.Cookie' crawlers/douyin/web/utils.py
```

Any hit means the patch has gone half-applied and `utils.py` needs the same
`cookie_provider` treatment. `check-patch-size.sh` runs this grep and fails on a
hit.

## 6. `web_signature.py` — Douyin's second signature on `aweme/detail` and `aweme/post`

New file `crawlers/douyin/web/web_signature.py` (ours entirely), called from two
lines in `web_crawler.py`: `fetch_one_video` and `fetch_user_post_videos` build
their URL with `signed_endpoint(...)` instead of appending `a_bogus` by hand.

**Why.** Since about October 2026 Douyin refuses those two endpoints without an
`x-secsdk-web-signature` query parameter (plus `uifid`, `verifyFp`, `fp`,
`timestamp`), even with a valid cookie and a valid `a_bogus`. The refusal is
`403 Blocked by ArgusSecurityPlugin Uifid Not Found`, then `Signature Not Found`.
This parser maps every failure to an information-free HTTP 400, so from outside it
looks exactly like an expired cookie. `handler_user_profile` is not protected and
keeps working, which is what makes the symptom misleading.

**Source.** Algorithm and salt are from upstream v5
(`dtk/signing/native/websign.py`, Apache-2.0), recovered from Douyin's own SDK.
`tests/test_web_signature.py` pins four signatures computed by running that SDK in
Node, so the salt and the preimage order are checked against an independent oracle.

**When it goes stale.** If Douyin rotates the salt, or protects another path, the
same 403 returns and the fix is in this file. The `uifid` is read from the
`Cookie` header (`UIFID` / `UIFID_TEMP`), so the extension's cookie push must keep
carrying it. A cookie without one gets the old, unsigned URL.

**Upstream.** `upstream/main` is no longer this code: upstream rewrote the project
as v5 from an empty tree on 2026-09-07 and `v4` is a frozen branch. `git pull
upstream main` cannot merge into this fork; our base is `upstream/v4`
(merge-base `42784ff`). `scripts/check-patch-size.sh` still diffs against
`upstream/main` and will report meaningless numbers until it is pointed at
`upstream/v4`.
