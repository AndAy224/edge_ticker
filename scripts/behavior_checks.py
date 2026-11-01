"""Behaviour checks: the display's auto-pin and the admin's config saves, driven
in headless Chromium against a FIXTURE-MODE backend (it rewrites the backend's
config for each scenario and restores it at the end).

Both used to be wrong in ways only a live sequence shows — a pin that outlived
its module, an admin tab that silently reverted another tab's save — so each
scenario here is one such sequence, checked through the real pages.

    TICKER_FIXTURE=scripts/fixtures/display_snapshot.json \\
    TICKER_DB=/tmp/checks.db FINNHUB_KEY= HA_URL= \\
        .venv/bin/uvicorn backend.main:app --port 8081
    (cd frontend && npm run build)
    .venv/bin/python scripts/behavior_checks.py              # both suites
    .venv/bin/python scripts/behavior_checks.py --only admin

Exit status 1 when any check fails. The display suite uses the __wsmsg and
__pinstate debug hooks; the admin suite injects a pausable WebSocket and a
fetch that can fail or stall, before the admin's own scripts run.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import websockets

from layout_audit import FIXTURE, Page, chromium

ORDER = ["markets", "news", "sports", "opnsense", "launches", "weather"]


class Checks:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool]] = []

    def __call__(self, name: str, ok, detail="") -> None:
        self.results.append((name, bool(ok)))
        print(("PASS " if ok else "FAIL ") + name + (f"  {detail}" if detail and not ok else ""))


class Backend:
    def __init__(self, url: str) -> None:
        self.url = url
        self.http = httpx.Client(base_url=url, timeout=20)

    def config(self) -> dict:
        return self.http.get("/api/config").json()

    def put(self, mutate) -> None:
        config = self.config()
        mutate(config)
        self.http.put("/api/config", json=config).raise_for_status()

    def control(self, action: str) -> None:
        self.http.post("/api/control", json={"action": action}).raise_for_status()


def payload_msg(module: str, stage: dict) -> str:
    message = {"type": "module", "payload": {"module": module, "stale": False, "tape": [], "stage": stage}}
    return f"__wsmsg(Object.assign({json.dumps(message)}, {{}}))"


# ---- display: auto-pin --------------------------------------------------------------


async def display_suite(page: Page, backend: Backend, check: Checks) -> None:
    fixture = json.loads(FIXTURE.read_text())["modules"]

    def base(c: dict) -> None:
        c["appearance"] = {"theme": "midnight", "layout": "rail-left"}
        c["rotation"] = {"order": ORDER, "interval_seconds": 3600}
        for m in ORDER:
            c["modules"].setdefault(m, {})["enabled"] = True
        c["modules"]["sports"]["auto_feature"] = True
        c["modules"]["launches"]["auto_feature"] = True
        c["modules"]["opnsense"].pop("auto_feature", None)

    backend.put(base)
    await page.cmd("Page.navigate", url=f"{backend.url}/display")
    await page.until("typeof __pinstate === 'function' && document.querySelectorAll('.stage-pane').length > 0", 15)
    await asyncio.sleep(1)

    async def state() -> dict:
        return await page.ev("__pinstate()")

    async def settle() -> None:
        await asyncio.sleep(1.3)  # the reconcile runs once a second

    async def failover(on: bool) -> None:
        await page.ev(payload_msg("opnsense", {"failover": on}))

    async def launches(stage: dict) -> None:
        await page.ev(payload_msg("launches", stage))

    await failover(True); await settle(); s = await state()
    check("failover auto-pins the network page",
          s["pinned"] and s["auto"] == "opnsense" and s["module"] == "opnsense" and s["badge"], s)

    backend.put(lambda c: c["rotation"].update(order=["opnsense", "weather", "markets", "news", "sports", "launches"]))
    await settle(); s = await state()
    check("reordering the rotation keeps the pin on its module", s["pinned"] and s["module"] == "opnsense", s)

    backend.put(lambda c: c["modules"]["opnsense"].update(enabled=False))
    await settle(); s = await state()
    check("disabling the module releases its pin",
          not s["pinned"] and s["auto"] is None and not s["badge"] and s["module"] != "opnsense", s)
    backend.put(lambda c: c["modules"]["opnsense"].update(enabled=True))
    await settle(); s = await state()
    check("re-enabled mid-failover, it pins again", s["pinned"] and s["auto"] == "opnsense", s)

    backend.put(lambda c: c["modules"]["opnsense"].update(auto_feature=False))
    await settle(); s = await state()
    check("switching auto_feature off releases the pin", not s["pinned"] and s["auto"] is None, s)
    backend.put(lambda c: c["modules"]["opnsense"].pop("auto_feature", None))
    await failover(False); await settle()

    live = json.loads(json.dumps(fixture["launches"]["stage"]))
    live["live"] = True
    await launches(live); await settle(); s = await state()
    check("a live launch auto-pins launches", s["pinned"] and s["auto"] == "launches", s)
    backend.put(lambda c: c["modules"]["launches"].update(auto_feature=False))
    await settle(); s = await state()
    check("launches' auto_feature off releases it too", not s["pinned"] and s["auto"] is None, s)
    await launches(dict(live, live=False))
    backend.put(lambda c: c["modules"]["launches"].update(auto_feature=True))

    backend.control("blank"); await settle()
    await failover(True); await settle(); s = await state()
    check("no pin while blanked", not s["pinned"] and "opnsense" in s["live"], s)
    backend.control("wake"); await settle(); s = await state()
    check("an event that began while blanked pins on wake", s["pinned"] and s["auto"] == "opnsense", s)

    backend.control("next"); await settle(); s = await state()
    check("moving the stage releases and dismisses the event", not s["pinned"] and "opnsense" in s["dismissed"], s)
    await asyncio.sleep(2); s = await state()
    check("a dismissed event doesn't take the stage back", not s["pinned"], s)
    await failover(False); await settle(); await failover(True); await settle(); s = await state()
    check("the next failover pins again", s["pinned"] and s["auto"] == "opnsense", s)

    backend.control("pin"); await settle(); s = await state()
    check("a long-press on an auto-pin unpins and dismisses", not s["pinned"] and "opnsense" in s["dismissed"], s)
    backend.control("pin"); await settle(); s = await state()
    check("a manual pin belongs to the user", s["pinned"] and s["auto"] is None, s)
    pinned_on = s["module"]
    backend.put(lambda c: c["rotation"].update(order=[m for m in c["rotation"]["order"] if m != pinned_on]))
    await settle(); s = await state()
    check("a manual pin on a module that left the rotation is released", not s["pinned"], s)
    backend.put(lambda c: c["rotation"].update(order=ORDER))
    await failover(False); await settle()

    backend.control("pin"); await settle()
    before = await state()
    backend.control("starship_test"); await asyncio.sleep(1)
    newer = json.loads(json.dumps(fixture["launches"]["stage"]))
    for launch in newer["launches"]:
        launch["provider"] = "NEWERPROVIDER"
    await launches(newer)
    still = await page.ev("document.querySelector('.stage-pane').textContent.includes('Starship')")
    await asyncio.sleep(20); after = await state()
    check("the Starship preview stays up when a real payload arrives", still)
    check("the preview gives the user's pin back", after["pinned"] == before["pinned"] and after["auto"] is None
          and after["badge"] == before["badge"], {"before": before, "after": after})
    await page.ev("__rotshow('launches')"); await asyncio.sleep(1.5)
    shown = await page.ev("document.querySelector('.stage-pane > .stage-layer:not(.exit):not(.pending)')"
                          ".textContent.includes('NEWERPROVIDER')")
    check("the preview puts back the newest real payload", shown)


# ---- admin: config saves --------------------------------------------------------------

HARNESS = r"""
(() => {
  const RealWS = window.WebSocket;
  window.__sockets = [];
  window.__wsPaused = false;
  window.WebSocket = class extends RealWS {
    constructor(...a) { super(...a); window.__sockets.push(this); }
    set onmessage(fn) { super.onmessage = (e) => { if (!window.__wsPaused) fn(e); }; }
    get onmessage() { return super.onmessage; }
  };
  const realFetch = window.fetch.bind(window);
  window.__failConfigGets = window.__failConfigGets || 0;
  window.__failPuts = 0; window.__putDelay = 0;
  window.fetch = async (url, opts = {}) => {
    const method = (opts.method || "GET").toUpperCase();
    if (String(url).endsWith("/api/config") && method === "GET" && window.__failConfigGets > 0) {
      window.__failConfigGets--; throw new TypeError("Failed to fetch");
    }
    if (method === "PUT") {
      if (window.__failPuts > 0) { window.__failPuts--; throw new TypeError("Failed to fetch"); }
      if (window.__putDelay) await new Promise((r) => setTimeout(r, window.__putDelay));
    }
    return realFetch(url, opts);
  };
})();
"""

HELPERS = r"""
window.__click = (sel, text) => {
  const el = [...document.querySelectorAll(sel)].find((e) => !text || e.textContent.trim().startsWith(text));
  if (!el) return false; el.click(); return true;
};
// Preact renders a newly picked tab after the click returns, so the tab
// helpers wait for it before touching its controls.
const rendered = () => new Promise((r) => setTimeout(r, 80));
window.__tab = async (name) => { __click("nav button", name); await rendered(); };
window.__interval = async (n) => {
  await __tab("Modules");
  const input = [...document.querySelectorAll("label.field")]
    .find((l) => l.textContent.includes("Stage interval")).querySelector("input");
  if (n === undefined) return input.value;
  input.value = String(n); input.dispatchEvent(new Event("input", { bubbles: true }));
  await rendered(); return true;
};
window.__theme = async (name) => {
  await __tab("Appearance");
  const ok = __click(".theme-card", name); await rendered(); return ok;
};
window.__save = () => __click("button.primary");
window.__status = () => document.querySelector(".save-status")?.textContent ?? "";
window.__saveEnabled = () => !document.querySelector("button.primary").disabled;
"""


async def admin_suite(a: Page, b: Page, backend: Backend, check: Checks) -> None:
    async def open_admin(page: Page) -> None:
        await page.cmd("Page.navigate", url=f"{backend.url}/admin")
        await page.until("!!document.querySelector('button.primary')", 25)
        await page.ev(HELPERS)

    # A load that fails says so and retries, instead of "loading config…" forever.
    await a.cmd("Page.addScriptToEvaluateOnNewDocument", source="window.__failConfigGets = 2;")
    await a.cmd("Page.navigate", url=f"{backend.url}/admin")
    await a.until("!!document.querySelector('.loading')", 10)
    await asyncio.sleep(0.3)
    loading = await a.ev("document.querySelector('.loading')?.textContent ?? ''")
    await a.until("!!document.querySelector('button.primary')", 25)
    await a.ev(HELPERS)
    check("a failed config load is shown and retried", "retrying" in loading, loading)
    await open_admin(b)

    await a.ev("__theme('Frost')")
    await b.ev("__interval(41)")
    await asyncio.sleep(0.2)
    await b.ev("__save()"); await asyncio.sleep(1.5)
    status = await a.ev("__status()")
    check("another tab's save reaches a tab with edits", "changed elsewhere" in status, status)
    await a.ev("__save()"); await asyncio.sleep(1.5)
    s = backend.config()
    check("saving that tab keeps both edits",
          s["appearance"]["theme"] == "frost" and s["rotation"]["interval_seconds"] == 41,
          (s["appearance"]["theme"], s["rotation"]["interval_seconds"]))

    await a.ev("window.__wsPaused = true")
    await a.ev("__theme('Amber')")
    backend.put(lambda c: c["rotation"].update(interval_seconds=42))  # unseen by A
    await asyncio.sleep(0.5)
    await a.ev("__save()"); await asyncio.sleep(1.5)
    status, s = await a.ev("__status()"), backend.config()
    check("a save from a stale copy is refused and overwrites nothing",
          "changed elsewhere" in status and s["rotation"]["interval_seconds"] == 42
          and s["appearance"]["theme"] == "frost", (status, s["appearance"]["theme"]))
    await a.ev("__save()"); await asyncio.sleep(1.5)
    s = backend.config()
    check("saving again after the merge keeps both",
          s["appearance"]["theme"] == "amber" and s["rotation"]["interval_seconds"] == 42,
          (s["appearance"]["theme"], s["rotation"]["interval_seconds"]))
    await a.ev("window.__wsPaused = false")

    await b.ev("window.__wsPaused = true")
    backend.put(lambda c: c["rotation"].update(interval_seconds=43))
    await asyncio.sleep(0.5)
    await b.ev("window.__wsPaused = false; window.__sockets.at(-1).close()")
    await asyncio.sleep(4.5)  # the admin reconnects after 3 s
    shown = await b.ev("__interval()")
    check("a clean tab adopts the snapshot after a reconnect", shown == "43", shown)

    await open_admin(a)
    await a.ev("window.__putDelay = 1500; __interval(44)")
    await asyncio.sleep(0.2)
    await a.ev("__save()"); await asyncio.sleep(0.3)
    await a.ev("__theme('Glance')")
    await asyncio.sleep(2.5)
    s = backend.config()
    check("an in-flight save sends what was on screen at the click",
          s["rotation"]["interval_seconds"] == 44 and s["appearance"]["theme"] == "amber",
          (s["rotation"]["interval_seconds"], s["appearance"]["theme"]))
    check("an edit made during the save stays unsaved", await a.ev("__saveEnabled()"))
    await a.ev("window.__putDelay = 0; window.__failPuts = 1; __save()")
    await asyncio.sleep(0.8)
    status = await a.ev("__status()")
    check("a save the backend never answers is reported, edits kept",
          "didn't answer" in status and await a.ev("__saveEnabled()"), status)
    await a.ev("__save()"); await asyncio.sleep(1.5)
    check("and saving again goes through", backend.config()["appearance"]["theme"] == "glance")


# ---- runner ---------------------------------------------------------------------------


async def run(args) -> int:
    backend = Backend(args.url)
    if not backend.http.get("/api/health").json().get("fixture"):
        sys.exit(f"{args.url} is not a fixture-mode backend — refusing to rewrite its config")
    original = backend.config()
    check = Checks()

    home_snap = Path.home() / "snap" / "chromium" / "common"
    profile = tempfile.mkdtemp(prefix="behavior-", dir=home_snap if home_snap.exists() else None)
    env = dict(os.environ, XDG_RUNTIME_DIR=os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    port = 9900 + os.getpid() % 90
    proc = subprocess.Popen(
        [chromium(), "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
         "--window-size=2560,720", "about:blank"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    sockets = []
    try:
        for _ in range(60):
            try:
                httpx.get(f"http://127.0.0.1:{port}/json/version")
                break
            except httpx.HTTPError:
                time.sleep(0.5)
        else:
            sys.exit("chromium did not start")

        async def new_page(harness: str | None = None) -> Page:
            target = httpx.put(f"http://127.0.0.1:{port}/json/new?about:blank").json()
            ws = await websockets.connect(target["webSocketDebuggerUrl"], max_size=2**27)
            sockets.append(ws)
            page = Page(ws)
            await page.cmd("Emulation.setDeviceMetricsOverride", width=2560, height=720,
                           deviceScaleFactor=1, mobile=False)
            await page.cmd("Page.enable")
            if harness:
                await page.cmd("Page.addScriptToEvaluateOnNewDocument", source=harness)
            return page

        if args.only in (None, "display"):
            print("-- display: auto-pin")
            await display_suite(await new_page(), backend, check)
            backend.http.put("/api/config", json=original)
        if args.only in (None, "admin"):
            print("-- admin: config saves")
            await admin_suite(await new_page(HARNESS), await new_page(HARNESS), backend, check)
    finally:
        for ws in sockets:
            await ws.close()
        proc.terminate()
        proc.wait(10)
        shutil.rmtree(profile, ignore_errors=True)
        backend.http.put("/api/config", json=original)

    failed = [name for name, ok in check.results if not ok]
    print(f"\n{len(check.results) - len(failed)}/{len(check.results)} passed")
    return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default=os.environ.get("TICKER_URL", "http://127.0.0.1:8081"))
    parser.add_argument("--only", choices=["display", "admin"])
    sys.exit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
