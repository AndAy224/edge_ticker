"""News collector — RSS via feedparser, deduped by GUID, source-interleaved.

Feeds are merged round-robin (newest of each feed, then second-newest, …) so
low-frequency sources aren't starved off the screen by high-frequency ones.
Polls use ETag/Last-Modified conditional GETs; a 304 reuses the cached items.
Per-feed fetch status is exposed through status() for the admin Sources tab.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import datetime, timezone

import feedparser

from ..state import ModulePayload, TapeItem
from .base import Collector

log = logging.getLogger(__name__)

DEFAULT_FEEDS = [
    {"name": "BBC World", "url": "https://feeds.bbci.co.uk/news/world/rss.xml"},
    {"name": "Hacker News", "url": "https://hnrss.org/frontpage"},
]

SUMMARY_MAX_CHARS = 360
BREAKING_MINUTES = 15


def _teaser(raw: str) -> str:
    """RSS summaries arrive as HTML — reduce to a plain-text teaser."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= SUMMARY_MAX_CHARS:
        return text
    return text[:SUMMARY_MAX_CHARS].rsplit(" ", 1)[0] + "…"


class NewsCollector(Collector):
    name = "news"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.feeds: list[dict] = self.module_config.get("feeds", DEFAULT_FEEDS)
        self.interval = float(self.module_config.get("poll_seconds", 300))
        self.keep = int(self.module_config.get("keep", 30))
        # Conditional-GET state and last-good items, keyed by feed URL.
        self._http_cache: dict[str, tuple[str | None, str | None]] = {}
        self._feed_items: dict[str, list[dict]] = {}
        self.feed_status: dict[str, dict] = {}

    async def fetch(self) -> list[dict]:
        results = await asyncio.gather(
            *(asyncio.to_thread(self._parse_feed, feed) for feed in self.feeds),
            return_exceptions=True,
        )
        per_feed: list[list[dict]] = []
        failures = 0
        now = datetime.now(timezone.utc).isoformat()
        for feed, result in zip(self.feeds, results):
            url = feed.get("url", "")
            entry = {"name": feed.get("name", ""), "url": url, "checked_at": now}
            if isinstance(result, BaseException):
                failures += 1
                log.debug("feed failed %s: %s", url, result)
                entry.update(ok=False, error=str(result), items=0, cached=False)
            else:
                items, cached = result
                per_feed.append(items)
                entry.update(ok=True, error=None, items=len(items), cached=cached)
            self.feed_status[url] = entry
        # Drop status entries for feeds removed from config.
        urls = {f.get("url", "") for f in self.feeds}
        self.feed_status = {u: s for u, s in self.feed_status.items() if u in urls}
        if failures == len(self.feeds) and self.feeds:
            raise RuntimeError("all feeds failed")

        # Round-robin merge: newest of each feed, then second-newest, … so
        # every source gets visible slots regardless of publish frequency.
        for items in per_feed:
            items.sort(key=lambda i: i["published"] or "", reverse=True)
        seen: set[str] = set()
        merged: list[dict] = []
        for rank in range(max((len(i) for i in per_feed), default=0)):
            for items in per_feed:
                if rank < len(items) and items[rank]["guid"] not in seen:
                    seen.add(items[rank]["guid"])
                    merged.append(items[rank])
        return merged[: self.keep]

    def _parse_feed(self, feed: dict) -> tuple[list[dict], bool]:
        """Returns (items, served_from_cache). Runs in a thread."""
        url = feed["url"]
        etag, modified = self._http_cache.get(url, (None, None))
        parsed = feedparser.parse(url, etag=etag, modified=modified)
        if getattr(parsed, "status", None) == 304:
            return self._feed_items.get(url, []), True
        if parsed.bozo and not parsed.entries:
            raise RuntimeError(f"unreadable feed: {url}")
        items = []
        for entry in parsed.entries[:20]:
            published = None
            for key in ("published_parsed", "updated_parsed"):
                value = entry.get(key)
                if value:
                    published = datetime(*value[:6], tzinfo=timezone.utc).isoformat()
                    break
            title = (entry.get("title") or "").strip()
            if not title:
                continue
            items.append(
                {
                    "guid": entry.get("id") or entry.get("link") or title,
                    "title": title,
                    "link": entry.get("link"),
                    "source": feed.get("name") or parsed.feed.get("title", "RSS"),
                    "published": published,
                    "summary": _teaser(
                        entry.get("summary") or entry.get("description") or ""
                    ),
                }
            )
        self._http_cache[url] = (
            getattr(parsed, "etag", None),
            getattr(parsed, "modified", None),
        )
        self._feed_items[url] = items
        return items, False

    def status(self) -> dict:
        return super().status() | {"feeds": list(self.feed_status.values())}

    def shape(self, items: list[dict]) -> ModulePayload:
        cutoff = datetime.now(timezone.utc).timestamp() - BREAKING_MINUTES * 60
        tape = []
        for item in items[:10]:
            fresh = (
                item["published"] is not None
                and datetime.fromisoformat(item["published"]).timestamp() >= cutoff
            )
            tape.append(
                TapeItem(
                    text=f"{item['source']}: {item['title']}",
                    accent="alert" if fresh else "neutral",
                )
            )
        return ModulePayload(module=self.name, stage={"items": items}, tape=tape)
