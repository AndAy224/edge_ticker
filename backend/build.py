"""What frontend build and code revision this backend is serving.

The build id of a page is the set of hashed assets its built index.html links.
Every WS snapshot carries it, and a display whose own document links a
different set reloads itself: a deploy used to need a manual `reload` control
POST — and that POST was lost if it arrived before the kiosk reconnected, so
the panel kept running the old bundle against the new backend. A reconnect
now always lands on the current build.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "frontend" / "dist"

_ASSET = re.compile(r'(?:src|href)="(/assets/[^"]+)"')
_cache: dict[str, tuple[float, str | None]] = {}


def build_id(page: str) -> str | None:
    """Sorted asset paths linked by dist/<page>/index.html (None if unbuilt).
    The display derives the same string from its own document."""
    index = DIST / page / "index.html"
    try:
        mtime = index.stat().st_mtime
    except OSError:
        return None
    cached = _cache.get(page)
    if cached and cached[0] == mtime:
        return cached[1]
    assets = sorted(set(_ASSET.findall(index.read_text(encoding="utf-8"))))
    value = "|".join(assets) or None
    _cache[page] = (mtime, value)
    return value


def builds() -> dict[str, str | None]:
    return {"display": build_id("display"), "admin": build_id("admin")}


def git_revision() -> str | None:
    """The checked-out commit, read straight from .git — the backend runs as
    `ticker` in a root-owned checkout, where the git CLI refuses to work
    ("dubious ownership")."""
    git = ROOT / ".git"
    try:
        head = (git / "HEAD").read_text().strip()
        if not head.startswith("ref: "):
            return head[:12]
        ref = head[5:]
        loose = git / ref
        if loose.exists():
            return loose.read_text().strip()[:12]
        for line in (git / "packed-refs").read_text().splitlines():
            if line.endswith(" " + ref):
                return line.split(" ", 1)[0][:12]
    except OSError:
        pass
    return None
