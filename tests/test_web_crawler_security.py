"""Security regression tests for crawlers/douyin/web/web_crawler.py.

These guard fixes that are NOT about cookie_provider.py's own logic (that
lives in tests/test_cookie_provider.py), but about closing an unsafe write
path that used to be reachable from web_crawler.py.
"""


def test_update_cookie_method_removed():
    """F4: update_cookie() used to write a caller-supplied cookie straight
    into the tracked config.yaml via yaml.dump(), and was reachable
    unauthenticated through POST /api/hybrid/update_cookie. We use a
    pull-model (Redis) for cookie rotation, so this method has no
    legitimate caller. It must be deleted outright -- not stubbed, not
    left as a no-op -- so it can never be silently re-merged with upstream
    and start writing to disk again."""
    from crawlers.douyin.web.web_crawler import DouyinWebCrawler

    assert not hasattr(DouyinWebCrawler, "update_cookie")
