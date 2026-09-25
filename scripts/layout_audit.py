"""Layout audit: render every module under every theme × layout and flag content
that is clipped part-way by its box — the half-visible row, the chip cut by the
pane edge. That class of bug was the most common fix in this repo's history,
and it was only ever caught by eye.

Runs against a FIXTURE-MODE backend, never a live one: it rewrites the
backend's appearance/rotation config for every combination (and restores it).

    # 1. a backend serving the recorded payloads (no upstream polling)
    TICKER_FIXTURE=scripts/fixtures/display_snapshot.json \\
    TICKER_DB=/tmp/audit.db FINNHUB_KEY= HA_URL= \\
        .venv/bin/uvicorn backend.main:app --port 8081
    # 2. a fresh build for it to serve:  (cd frontend && npm run build)
    # 3. the audit
    .venv/bin/python scripts/layout_audit.py                      # everything
    .venv/bin/python scripts/layout_audit.py --themes sovereign --layouts rail-left,split \\
        --modules proxmox,adsb --shots ~/audit-shots

Exit status 1 when anything is clipped, fails to render, or never arrives.
On the appliance, Chromium is the snap: screenshots and the profile must live
under $HOME (snap confinement), which is where this puts them.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import websockets

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "scripts" / "fixtures" / "display_snapshot.json"
THEMES_TS = ROOT / "frontend" / "shared" / "themes.ts"
NO_STAGE = {"weather_alerts"}  # rides the weather rail/stage, never its own pane

# Reports the outermost element in each stage layer that its nearest clipping
# ancestor cuts part-way. Wholly hidden is fine (the proxmox guest table wraps
# rows that don't fit out of view on purpose); partly visible is the bug.
PROBE = r"""
(() => {
  const out = [];
  // Map viewports clip tiles and tracks by design.
  const exempt = (el) => el.closest('.radar-viewport, .radar-map, .adsb-radar, .radar-scope, svg');
  const reported = new Set();
  for (const layer of document.querySelectorAll('.stage-pane > .stage-layer:not(.exit):not(.pending)')) {
    const pane = layer.parentElement.dataset.pane;
    const text = layer.textContent || '';
    if (/Waiting for .* data|couldn't be drawn/.test(text)) out.push({pane, el: 'layer', text: text.trim().slice(0, 60), by: 0});
    for (const el of layer.querySelectorAll('*')) {
      if (exempt(el)) continue;
      const r = el.getBoundingClientRect();
      if (r.width < 2 || r.height < 2) continue;
      const cs = getComputedStyle(el);
      if (cs.visibility === 'hidden' || cs.position === 'fixed') continue;
      let dup = false;
      for (let a = el.parentElement; a && a !== layer; a = a.parentElement) if (reported.has(a)) { dup = true; break; }
      if (dup) continue;
      for (let box = el.parentElement; box; box = box.parentElement) {
        const bs = getComputedStyle(box);
        const clips = bs.overflowX !== 'visible' || bs.overflowY !== 'visible' || box.classList.contains('stage-pane');
        if (clips) {
          const b = box.getBoundingClientRect();
          const inside = r.top >= b.top - 2 && r.bottom <= b.bottom + 2 && r.left >= b.left - 2 && r.right <= b.right + 2;
          const outside = r.bottom <= b.top + 1 || r.top >= b.bottom - 1 || r.right <= b.left + 1 || r.left >= b.right - 1;
          if (outside) break;
          if (!inside) {
            reported.add(el);
            const cls = typeof el.className === 'string' && el.className.trim() ? '.' + el.className.trim().split(/\s+/).join('.') : el.tagName.toLowerCase();
            out.push({pane, el: cls, text: (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 40),
                      by: Math.round(Math.max(b.top - r.top, r.bottom - b.bottom, b.left - r.left, r.right - b.right))});
            break;
          }
        }
        if (box.classList.contains('stage-pane')) break;
      }
    }
  }
  return out;
})()
"""


def theme_and_layout_ids() -> tuple[list[str], list[str]]:
    src = THEMES_TS.read_text(encoding="utf-8")

    def keys(block: str) -> list[str]:
        start = src.index(f"export const {block}")
        body = src[src.index("{", start) + 1 :]
        depth, out, i = 1, [], 0
        for m in re.finditer(r"[{}]|^  \"?([a-z][\w-]*)\"?:", body, re.M):
            if m.group(0) == "{":
                depth += 1
            elif m.group(0) == "}":
                depth -= 1
                if depth == 0:
                    break
            elif depth == 1:
                out.append(m.group(1))
        return out

    return keys("THEMES"), keys("LAYOUTS")


def chromium() -> str:
    for candidate in ("/snap/bin/chromium", "chromium", "chromium-browser", "google-chrome"):
        path = shutil.which(candidate) or (candidate if os.path.exists(candidate) else None)
        if path:
            return path
    sys.exit("no chromium found")


class Page:
    def __init__(self, ws):
        self.ws, self.n = ws, 0

    async def cmd(self, method: str, **params):
        self.n += 1
        mid = self.n
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            message = json.loads(await self.ws.recv())
            if message.get("id") == mid:
                return message.get("result", message.get("error"))

    async def ev(self, expr: str):
        r = await self.cmd("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        return (r or {}).get("result", {}).get("value")

    async def until(self, expr: str, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await self.ev(expr):
                return True
            await asyncio.sleep(0.1)
        return False


async def audit(args) -> int:
    client = httpx.Client(base_url=args.url, timeout=20)
    health = client.get("/api/health").json()
    if not health.get("fixture"):
        sys.exit(f"{args.url} is not a fixture-mode backend — refusing to rewrite its config")
    original = client.get("/api/config").json()

    all_themes, all_layouts = theme_and_layout_ids()
    themes = args.themes.split(",") if args.themes else all_themes
    layouts = args.layouts.split(",") if args.layouts else all_layouts
    fixture = json.loads(FIXTURE.read_text())["modules"]
    modules = args.modules.split(",") if args.modules else [m for m in fixture if m not in NO_STAGE]
    shots = Path(args.shots).expanduser() if args.shots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)

    home_snap = Path.home() / "snap" / "chromium" / "common"
    profile = tempfile.mkdtemp(prefix="layout-audit-", dir=home_snap if home_snap.exists() else None)
    env = dict(os.environ, XDG_RUNTIME_DIR=os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    port = 9400 + os.getpid() % 500
    proc = subprocess.Popen(
        [chromium(), "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
         "--window-size=2560,720", "--hide-scrollbars", "about:blank"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    findings: list[str] = []
    try:
        for _ in range(60):
            try:
                tabs = httpx.get(f"http://127.0.0.1:{port}/json").json()
                break
            except httpx.HTTPError:
                time.sleep(0.5)
        else:
            sys.exit("chromium did not start")
        target = next(t for t in tabs if t["type"] == "page")
        async with websockets.connect(target["webSocketDebuggerUrl"], max_size=2**27) as ws:
            page = Page(ws)
            # An attached session otherwise gets a 2560x577 viewport.
            await page.cmd("Emulation.setDeviceMetricsOverride", width=2560, height=720,
                           deviceScaleFactor=1, mobile=False)
            await page.cmd("Page.enable")
            await page.cmd("Page.navigate", url=f"{args.url}/display")
            await page.until("typeof __rotshow === 'function' && document.querySelector('.stage-pane')", 15)
            for theme in themes:
                for layout in layouts:
                    config = json.loads(json.dumps(original))
                    config.setdefault("appearance", {}).update(theme=theme, layout=layout)
                    config.setdefault("rotation", {}).update(order=modules, interval_seconds=3600)
                    for m in modules:
                        config.setdefault("modules", {}).setdefault(m, {})["enabled"] = True
                    r = client.put("/api/config", json=config)
                    if r.status_code != 200:
                        sys.exit(f"config rejected: {r.text}")
                    await page.until(
                        f"document.documentElement.dataset.theme === {json.dumps(theme)} && "
                        f"document.documentElement.dataset.layout === {json.dumps(layout)}"
                    )
                    for module in modules:
                        if await page.ev(f"__rotshow({json.dumps(module)})") is None:
                            findings.append(f"{theme}/{layout}/{module}: not in rotation (no renderer?)")
                            continue
                        # settle: the swap animation done, refreshes revealed, images in
                        await asyncio.sleep(0.2)
                        await page.until("!document.querySelector('.stage-layer.exit, .stage-layer.pending')", 3)
                        await asyncio.sleep(args.settle)
                        clips = await page.ev(PROBE) or []
                        for c in clips:
                            findings.append(
                                f"{theme}/{layout}/{module} pane {c['pane']}: {c['el']} "
                                f"cut by {c['by']}px — {c['text']!r}"
                            )
                        if shots:
                            shot = await page.cmd("Page.captureScreenshot", format="png")
                            (shots / f"{theme}-{layout}-{module}.png").write_bytes(base64.b64decode(shot["data"]))
                    print(f"{theme:10} {layout:11} {len(modules)} modules checked", flush=True)
    finally:
        proc.terminate()
        proc.wait(10)
        shutil.rmtree(profile, ignore_errors=True)
        client.put("/api/config", json=original)

    if findings:
        print(f"\n{len(findings)} finding(s):")
        for f in findings:
            print("  " + f)
        return 1
    print("\nno clipping found")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default=os.environ.get("TICKER_URL", "http://127.0.0.1:8081"))
    parser.add_argument("--themes", help="comma list (default: all in shared/themes.ts)")
    parser.add_argument("--layouts", help="comma list (default: all)")
    parser.add_argument("--modules", help="comma list (default: every module in the fixture)")
    parser.add_argument("--shots", help="directory for screenshots (under $HOME for snap chromium)")
    parser.add_argument("--settle", type=float, default=0.3, help="seconds to let a module settle")
    sys.exit(asyncio.run(audit(parser.parse_args())))


if __name__ == "__main__":
    main()
