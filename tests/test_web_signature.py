import hashlib
from urllib.parse import parse_qsl, unquote, urlsplit

import pytest

from crawlers.douyin.web import web_signature as ws


def _sign_url(url: str, cookie: str, stamp: int) -> str:
    pairs = parse_qsl(urlsplit(url).query, keep_blank_values=True)
    uifid = dict(pairs).get("uifid") or ws.pick_uifid(cookie)
    assert uifid is not None
    _query, signature = ws.sign(pairs, uifid, timestamp=stamp)
    return signature


def test_reproduces_signatures_computed_by_the_platforms_own_sdk():
    """Vectors produced by executing Douyin's real SDK (runtime_bundler_34.js)
    in Node and reading what it computed -- an oracle independent of this
    implementation, so a wrong salt or a wrong preimage order fails here rather
    than only against the live site. The uifid values are invented."""
    uifid = "aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff0011"
    vectors = [
        (
            "https://www.douyin.com/aweme/v1/web/aweme/detail/?device_platform=webapp&aid=6383&aweme_id=123",
            f"UIFID_TEMP={uifid}",
            1788848841,
            "6090b6162b96aeb107b51ab69140c764",
        ),
        (
            "https://www.douyin.com/x/?q=%E4%B8%AD&z=a b&e=%2F",
            "UIFID=AAAA1111; UIFID_TEMP=BBBB2222",
            1788848901,
            "a79dcd330ecba4d98016189c3ca313cd",
        ),
        ("https://www.douyin.com/x/", "UIFID=AAAA1111", 1788848901, "0479f3eb2297110d45bfbd96ccccb292"),
        # A uifid already in the query keeps its place; appending a second one
        # changes the preimage and the platform refuses it.
        (
            "https://www.douyin.com/x/?uifid=ZZZ&t=1",
            "UIFID=AAAA1111",
            1788848901,
            "df59a6887d4647239b9079d7d16ad784",
        ),
    ]
    for url, cookie, stamp, expected in vectors:
        assert _sign_url(url, cookie, stamp) == expected, url


def test_signature_is_a_pure_function_of_the_second():
    pairs = [("a", "1")]
    first = ws.sign(pairs, "U", timestamp=100)[1]
    assert ws.sign(pairs, "U", timestamp=100)[1] == first
    assert ws.sign(pairs, "U", timestamp=101)[1] != first


def test_the_query_is_serialized_the_way_it_is_hashed():
    query, signature = ws.sign([("z", "a b"), ("q", "中")], "UIFIDX", timestamp=1788848901)
    assert "a%20b" in query
    assert query.endswith(f"&x-secsdk-web-signature={signature}")
    covered = query.rsplit("&", 1)[0]
    assert hashlib.md5(f"UIFIDX_1788848901_{ws.SALT}_{covered}".encode()).hexdigest() == signature


@pytest.mark.parametrize(
    "cookie, expected",
    [
        ("", None),
        ("ttwid=1|x", None),
        ("UIFID_TEMP=abc", "abc"),
        ("a=1;  UIFID=xyz ; b=2", "xyz"),
        ("UIFID_TEMP=temp; uifid=plain", "plain"),  # first name in the SDK's own order wins
        ("UIFID=", None),
    ],
)
def test_pick_uifid(cookie, expected):
    assert ws.pick_uifid(cookie) == expected


UNSIGNED = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
PARAMS = {"aid": "6383", "aweme_id": "123", "msToken": ""}
A_BOGUS_QUOTED = "Dj%2FxQ-9%3D%3D"  # what BogusManager.ab_model_2_endpoint returns: percent-encoded


def test_signed_endpoint_appends_the_visitor_params_in_the_platforms_order():
    cookie = "UIFID=VISITOR; s_v_web_id=verify_abc"
    url = ws.signed_endpoint(UNSIGNED, PARAMS, A_BOGUS_QUOTED, cookie, timestamp=1788848901)
    base, _, query = url.partition("?")
    assert base == UNSIGNED
    names = [k for k, _ in parse_qsl(query, keep_blank_values=True)]
    assert names == [
        "aid", "aweme_id", "msToken", "a_bogus", "verifyFp", "fp", "uifid", "timestamp",
        "x-secsdk-web-signature",
    ]
    values = dict(parse_qsl(query, keep_blank_values=True))
    assert values["a_bogus"] == unquote(A_BOGUS_QUOTED)
    assert values["verifyFp"] == values["fp"] == "verify_abc"
    assert values["uifid"] == "VISITOR"
    assert values["msToken"] == ""


def test_signed_endpoint_signs_exactly_the_bytes_it_sends():
    url = ws.signed_endpoint(UNSIGNED, PARAMS, A_BOGUS_QUOTED, "UIFID=V; s_v_web_id=vfy", timestamp=1788848901)
    covered, _, signature = url.partition("?")[2].rpartition("&x-secsdk-web-signature=")
    assert hashlib.md5(f"V_1788848901_{ws.SALT}_{covered}".encode()).hexdigest() == signature


def test_without_verify_fp_cookie_those_params_are_omitted_but_still_signed():
    url = ws.signed_endpoint(UNSIGNED, PARAMS, A_BOGUS_QUOTED, "UIFID=V", timestamp=1788848901)
    query = url.partition("?")[2]
    assert "verifyFp" not in query and "fp=" not in query
    assert "x-secsdk-web-signature=" in query


def test_without_a_visitor_id_the_legacy_url_is_returned_unchanged():
    """A jar with no UIFID gets the old query untouched rather than a signature
    over a missing visitor: unprotected endpoints keep working and the protected
    ones fail with Douyin naming `uifid`, which says more than anything invented
    here would."""
    url = ws.signed_endpoint(UNSIGNED, PARAMS, A_BOGUS_QUOTED, "ttwid=1|x")
    assert url == f"{UNSIGNED}?aid=6383&aweme_id=123&msToken=&a_bogus={A_BOGUS_QUOTED}"


def test_the_two_protected_crawler_methods_use_the_signed_endpoint(monkeypatch):
    """Wiring, not just the helper: fetch_one_video and fetch_user_post_videos
    must hand BaseCrawler the signed URL. The captured value is the observable
    outcome -- a spy on signed_endpoint alone would pass even if its result were
    thrown away."""
    import asyncio

    from crawlers.douyin.web import web_crawler as wc

    seen = []

    class FakeBase:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def fetch_get_json(self, endpoint):
            seen.append(endpoint)
            return {"ok": True}

    async def fake_headers(self):
        return {
            "headers": {"User-Agent": "UA", "Cookie": "UIFID=VISITOR; s_v_web_id=vfy"},
            "proxies": {},
        }

    monkeypatch.setattr(wc, "BaseCrawler", FakeBase)
    monkeypatch.setattr(wc.DouyinWebCrawler, "get_douyin_headers", fake_headers)
    monkeypatch.setattr(wc.BogusManager, "ab_model_2_endpoint", staticmethod(lambda params, ua: "AB"))

    crawler = wc.DouyinWebCrawler()
    asyncio.run(crawler.fetch_one_video("123"))
    asyncio.run(crawler.fetch_user_post_videos("sec", 0, 5))

    assert len(seen) == 2
    for endpoint in seen:
        assert "x-secsdk-web-signature=" in endpoint
        assert "uifid=VISITOR" in endpoint
