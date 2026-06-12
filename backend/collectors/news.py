"""News collector — RSS via feedparser, deduped by GUID, newest first."""
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

SUMMARY_MAX_CHARS = 360


def _teaser(raw: str) -> str:
    """RSS summaries arrive as HTML — reduce to a plain-text teaser."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= SUMMARY_MAX_CHARS:
        return text
    return text[:SUMMARY_MAX_CHARS].rsplit(" ", 1)[0] + "…"

DEFAULT_FEEDS = [
    {"name": "BBC World", "url": "https://feeds.bbci.co.uk/news/world/rss.xml"},
    {"name": "Hacker News", "url": "https://hnrss.org/frontpage"},
]


class NewsCollector(Collector):
    name = "news"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.feeds: list[dict] = self.module_config.get("feeds", DEFAULT_FEEDS)
        self.interval = float(self.module_config.get("poll_seconds", 300))
        self.keep = int(self.module_config.get("keep", 30))

    async def fetch(self) -> list[dict]:
        results = await asyncio.gather(
            *(asyncio.to_thread(self._parse_feed, feed) for feed in self.feeds),
            return_exceptions=True,
        )
        items: list[dict] = []
        failures = 0
        for feed, result in zip(self.feeds, results):
            if isinstance(result, BaseException):
                failures += 1
                log.debug("feed failed %s: %s", feed.get("url"), result)
            else:
                items.extend(result)
        if failures == len(self.feeds) and self.feeds:
            raise RuntimeError("all feeds failed")

        seen: set[str] = set()
        deduped: list[dict] = []
        for item in sorted(items, key=lambda i: i["published"] or "", reverse=True):
            if item["guid"] in seen:
                continue
            seen.add(item["guid"])
            deduped.append(item)
        return deduped[: self.keep]

    @staticmethod
    def _parse_feed(feed: dict) -> list[dict]:
        parsed = feedparser.parse(feed["url"])
        if parsed.bozo and not parsed.entries:
            raise RuntimeError(f"unreadable feed: {feed['url']}")
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
        return items

    def shape(self, items: list[dict]) -> ModulePayload:
        tape = [
            TapeItem(text=f"{item['source']}: {item['title']}") for item in items[:10]
        ]
        return ModulePayload(module=self.name, stage={"items": items}, tape=tape)
