"""backend/collectors/news.py: httpx feed download (timeout, conditional GET,
304 -> cached items, http(s)-only URLs), parsing from bytes, merge + shape."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from backend.collectors import news as news_module
from backend.collectors.news import NewsCollector, _teaser
from helpers import load_bytes

BBC = "https://feeds.bbci.co.uk/news/world/rss.xml"
SMALL = "https://example.test/small.xml"
ETAG = '"bbc-v1"'
MODIFIED = "Fri, 25 Sep 2026 12:32:45 GMT"


def rss(items: list[tuple[str, str, datetime]]) -> bytes:
    body = "".join(
        f"<item><title>{title}</title><guid>{guid}</guid><link>https://example.test/{guid}</link>"
        f"<pubDate>{format_datetime(when)}</pubDate><description>&lt;p&gt;Hello &amp;amp; "
        f"welcome&lt;/p&gt;</description></item>"
        for guid, title, when in items
    )
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>Small</title>'
        f"{body}</channel></rss>"
    ).encode()


def config_with(feeds: list[dict]) -> dict:
    return {"modules": {"news": {"feeds": feeds, "keep": 30}}}


class Feeds:
    """Serves BBC (with validators, honouring If-None-Match) and a small feed."""

    def __init__(self) -> None:
        self.small = rss(
            [
                ("s1", "Small story one", datetime(2026, 9, 25, 11, 0, tzinfo=timezone.utc)),
                ("s2", "Small story two", datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc)),
            ]
        )
        self.bbc_down = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == BBC:
            if self.bbc_down:
                return httpx.Response(502)
            if request.headers.get("if-none-match") == ETAG:
                return httpx.Response(304)
            return httpx.Response(
                200,
                content=load_bytes("bbc_world.xml"),
                headers={"ETag": ETAG, "Last-Modified": MODIFIED, "Content-Type": "text/xml"},
            )
        if url == SMALL:
            return httpx.Response(200, content=self.small, headers={"Content-Type": "application/rss+xml"})
        return httpx.Response(404)


@pytest.fixture
def feeds(http) -> Feeds:
    handler = Feeds()
    http.handler = handler
    return handler


async def test_parses_recorded_feed(http, feeds):
    collector = NewsCollector(config_with([{"name": "BBC World", "url": BBC}]))
    items = await collector.fetch()
    assert len(items) == 5
    first = items[0]
    assert first["source"] == "BBC World"
    assert first["guid"].startswith("https://www.bbc.co.uk/news/articles/")
    assert first["title"] and first["link"].startswith("https://www.bbc.co.uk/")
    assert first["published"].endswith("+00:00")
    assert "<" not in first["summary"]
    published = [i["published"] for i in items]
    assert published == sorted(published, reverse=True)
    client = http.client_kwargs[0]
    assert client["timeout"] == news_module.FEED_TIMEOUT_SECONDS
    assert client["follow_redirects"] is True
    assert http.requests[0].headers["user-agent"] == news_module.FEED_HEADERS["User-Agent"]
    assert collector.feed_status[BBC]["ok"] is True and collector.feed_status[BBC]["items"] == 5


async def test_conditional_get_serves_cached_items_on_304(http, feeds):
    collector = NewsCollector(config_with([{"name": "BBC World", "url": BBC}]))
    first = await collector.fetch()
    assert "if-none-match" not in http.requests[0].headers
    second = await collector.fetch()
    request = http.requests[1]
    assert request.headers["if-none-match"] == ETAG
    assert request.headers["if-modified-since"] == MODIFIED
    assert second == first
    assert collector.feed_status[BBC]["cached"] is True


async def test_non_http_urls_are_rejected_without_a_request(http, feeds):
    collector = NewsCollector(
        config_with(
            [
                {"name": "BBC World", "url": BBC},
                {"name": "Local", "url": "file:///etc/passwd"},
                {"name": "Bare", "url": "/etc/hosts"},
            ]
        )
    )
    items = await collector.fetch()
    assert len(items) == 5
    assert [str(r.url) for r in http.requests] == [BBC]
    status = collector.feed_status["file:///etc/passwd"]
    assert status["ok"] is False and "not an http(s) feed URL" in status["error"]
    assert collector.degraded == "2/3 feeds failing"


async def test_all_feeds_failing_raises(http, feeds):
    feeds.bbc_down = True
    collector = NewsCollector(config_with([{"name": "BBC World", "url": BBC}]))
    with pytest.raises(RuntimeError, match="all feeds failed"):
        await collector.fetch()
    assert collector.feed_status[BBC]["ok"] is False


async def test_timeout_is_a_feed_failure(http, feeds):
    def handler(request):
        if str(request.url) == SMALL:
            raise httpx.ReadTimeout("stalled", request=request)
        return feeds(request)

    http.handler = handler
    collector = NewsCollector(config_with([{"name": "BBC World", "url": BBC}, {"name": "Small", "url": SMALL}]))
    items = await collector.fetch()
    assert len(items) == 5
    assert collector.degraded == "1/2 feeds failing"
    assert collector.feed_status[SMALL]["ok"] is False


async def test_unreadable_feed_fails_only_that_feed(http, feeds):
    def handler(request):
        if str(request.url) == SMALL:
            return httpx.Response(200, content=b"<html>definitely not a feed")
        return feeds(request)

    http.handler = handler
    collector = NewsCollector(config_with([{"name": "BBC World", "url": BBC}, {"name": "Small", "url": SMALL}]))
    await collector.fetch()
    assert "unreadable feed" in collector.feed_status[SMALL]["error"]


async def test_round_robin_merge_and_dedupe(http, feeds):
    # The small feed repeats BBC's newest guid: it must appear once.
    bbc_items = NewsCollector(config_with([]))._parse_feed({"url": BBC}, load_bytes("bbc_world.xml"), {})
    dup_guid = max(bbc_items, key=lambda i: i["published"])["guid"]
    feeds.small = rss(
        [
            ("s1", "Small story one", datetime(2026, 9, 25, 11, 0, tzinfo=timezone.utc)),
            (dup_guid, "Same story, other feed", datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc)),
        ]
    )
    collector = NewsCollector(config_with([{"name": "BBC World", "url": BBC}, {"name": "Small", "url": SMALL}]))
    items = await collector.fetch()
    assert [i["source"] for i in items[:4]] == ["BBC World", "Small", "BBC World", "BBC World"]
    guids = [i["guid"] for i in items]
    assert len(guids) == len(set(guids)) == 6
    collector.keep = 3
    assert len(await collector.fetch()) == 3


def test_shape_marks_breaking_items():
    collector = NewsCollector(config_with([]))
    now = datetime.now(timezone.utc)
    items = [
        {"guid": "a", "title": "Fresh", "source": "Wire", "published": (now - timedelta(minutes=5)).isoformat()},
        {"guid": "b", "title": "Old", "source": "Wire", "published": (now - timedelta(hours=2)).isoformat()},
        {"guid": "c", "title": "Undated", "source": "Wire", "published": None},
    ]
    payload = collector.shape(items)
    assert [(t.text, t.accent) for t in payload.tape] == [
        ("Wire: Fresh", "alert"),
        ("Wire: Old", "neutral"),
        ("Wire: Undated", "neutral"),
    ]
    assert payload.stage == {"items": items}


def test_shape_tape_capped_at_ten():
    collector = NewsCollector(config_with([]))
    items = [{"guid": str(i), "title": f"t{i}", "source": "S", "published": None} for i in range(15)]
    assert len(collector.shape(items).tape) == 10


def test_teaser_strips_html_and_truncates():
    assert _teaser("<p>Hello &amp; <b>welcome</b></p>\n\n") == "Hello & welcome"
    long = "word " * 200
    teaser = _teaser(long)
    assert teaser.endswith("…") and len(teaser) <= news_module.SUMMARY_MAX_CHARS + 1
