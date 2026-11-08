"""Douyin's `x-secsdk-web-signature`, the second signature on protected endpoints.

Douyin sign-protects `aweme/detail` and `aweme/post` (the ones behind
`fetch_one_video` and `fetch_user_post_videos`). `a_bogus` alone is accepted on
them, but a request without this signature is refused with
`403 Blocked by ArgusSecurityPlugin Uifid Not Found`, then `Signature Not Found`
once `uifid` is present. The parser turns that 403 into an information-free 400.

Algorithm and constants are from upstream Douyin_TikTok_Download_API v5
(`dtk/signing/native/websign.py`, Apache-2.0), who recovered them from Douyin's
own SDK (`runtime_bundler_34.js`, strategy `webSign`) and checked them against
signatures a real browser produced. The salt is per ByteDance project; if
Douyin rotates it, or protects further paths, the symptom is the same 403 and
the fix is here.

    query = URLSearchParams(everything except the signature)
    sig   = md5(f"{uifid}_{timestamp}_{SALT}_{query}")

The same string is hashed and sent, so ONE encoder builds the whole query.
Only the query parameters are needed; the SDK's `uifid` / `x-secsdk-web-*`
headers are optional and are not sent.
"""

import hashlib
import time
from urllib.parse import quote, unquote, urlencode

SALT = "A96D855A08C0A9707F8BEF0D9A527E4E"
SIGNATURE_PARAM = "x-secsdk-web-signature"

# The SDK looks for the visitor id under these names, first non-empty wins.
_UIFID_COOKIE_NAMES = ("uifid", "uifid_temp", "uifidtemp", "UIFID", "UIFID_TEMP", "UIFIDTEMP")
# The cookie that IS both `verifyFp` and `fp`.
_VERIFY_FP_COOKIE = "s_v_web_id"


def _cookie_value(cookie: str, name: str):
    for part in (cookie or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name and value:
            return value
    return None


def pick_uifid(cookie: str):
    """The visitor id the signature is bound to, from a `Cookie` header value."""
    for name in _UIFID_COOKIE_NAMES:
        value = _cookie_value(cookie, name)
        if value:
            return value
    return None


def _encode(pairs) -> str:
    # `*-._` are the characters URLSearchParams leaves alone.
    return "&".join(f"{quote(k, safe='*-._')}={quote(v, safe='*-._')}" for k, v in pairs)


def sign(pairs, uifid: str, timestamp=None):
    """Return `(query, signature)`; `query` already ends with the signature.

    `uifid` and `timestamp` are appended here because the signature covers
    them. A `uifid` already in `pairs` keeps its place and is not duplicated.
    """
    stamp = str(int(time.time() if timestamp is None else timestamp))
    covered = list(pairs)
    if not any(name == "uifid" for name, _ in covered):
        covered.append(("uifid", uifid))
    covered.append(("timestamp", stamp))
    query = _encode(covered)
    signature = hashlib.md5(f"{uifid}_{stamp}_{SALT}_{query}".encode()).hexdigest()
    return f"{query}&{SIGNATURE_PARAM}={signature}", signature


def signed_endpoint(base_url: str, params: dict, a_bogus: str, cookie: str, timestamp=None) -> str:
    """The request URL for a protected endpoint.

    `a_bogus` is the percent-encoded value `BogusManager.ab_model_2_endpoint`
    returns. With no visitor id in the cookie the pre-existing URL is returned
    unchanged, rather than a signature over a missing visitor: unprotected
    endpoints keep working and protected ones fail with Douyin naming `uifid`.
    """
    uifid = pick_uifid(cookie)
    if not uifid:
        return f"{base_url}?{urlencode(params)}&a_bogus={a_bogus}"
    pairs = [(k, str(v)) for k, v in params.items()]
    pairs.append(("a_bogus", unquote(a_bogus)))
    verify_fp = _cookie_value(cookie, _VERIFY_FP_COOKIE)
    if verify_fp:
        pairs += [("verifyFp", verify_fp), ("fp", verify_fp)]
    query, _ = sign(pairs, uifid, timestamp=timestamp)
    return f"{base_url}?{query}"
