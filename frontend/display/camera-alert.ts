// Full-screen camera takeover: a theme-aware alert entrance, then a wall of
// live MJPEG tiles proxied by the backend, a pulsing banner with a countdown,
// and auto-dismiss. Reusable for any `camera_alert` producer (HA doors, motion,
// doorbell) — everything alert-specific arrives in the event.
//
// The tiles are <img>s holding long-lived HTTP connections. dismiss() MUST kill
// them: detaching the node alone is not reliable in Chromium, and a leaked
// stream permanently burns one of its six per-host connections. See stopFeed().
//
// All animation is CSS (transform/opacity, the compositor path); the only JS
// that runs per second is the countdown digit.

import { WEATHER_ICONS } from "./icons";
import type { CameraAlertEvent } from "./types";

const ENTRANCE_MS = 900;
const SNAPSHOT_POLL_MS = 1000;
const STREAM_WATCHDOG_MS = 6000; // no first frame by now → fall back to stills
const MAX_TILE_FAILURES = 3;
const MAX_QUEUE = 1; // a 30s-stale motion event isn't worth showing
const PREEMPT_RETRY_MS = 3000;
const BLANK_GIF =
  "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7";

function esc(value: unknown): string {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!,
  );
}

export class CameraAlertOverlay {
  private queue: CameraAlertEvent[] = [];
  private current: CameraAlertEvent | null = null;
  private timers: number[] = [];
  private intervals: number[] = [];
  private endsAt = 0;

  constructor(
    private el: HTMLElement,
    private isBlanked: () => boolean,
    private wakeDisplay: () => void,
    private isPreempted: () => boolean,
    private onOpenChange: (open: boolean) => void,
  ) {
    el.addEventListener("pointerdown", () => this.dismiss());
  }

  isOpen(): boolean {
    return this.current !== null;
  }

  show(event: CameraAlertEvent): void {
    if (!event?.cameras?.length) return;
    if (this.isBlanked()) {
      // Waking is a per-event decision, not a display policy: a quiet
      // "package delivered" sets wake:false and stays suppressed.
      if (event.wake === false) return;
      this.wakeDisplay();
    }
    if (this.current) {
      // The same source firing again restarts the clock rather than stacking a
      // second identical takeover behind this one.
      if (this.current.key === event.key) this.extend(event);
      else if (this.queue.length < MAX_QUEUE) this.queue.push(event);
      return;
    }
    if (this.isPreempted()) {
      // A severe-weather takeover is life-safety and outranks a door. Retry
      // once, then drop.
      if (this.queue.length < MAX_QUEUE) {
        this.queue.push(event);
        this.timers.push(window.setTimeout(() => this.drain(), PREEMPT_RETRY_MS));
      }
      return;
    }
    this.play(event);
  }

  private extend(event: CameraAlertEvent): void {
    const seconds = event.duration_seconds ?? 30;
    this.endsAt = Date.now() + seconds * 1000;
    for (const t of this.timers) clearTimeout(t);
    this.timers = [];
    this.timers.push(window.setTimeout(() => this.dismiss(), seconds * 1000));
    const bar = this.el.querySelector<HTMLElement>(".calert-progress");
    if (bar) {
      bar.style.transition = "none";
      bar.style.transform = "scaleX(1)";
      // Two frames: one to commit the reset, one to start the new run.
      requestAnimationFrame(() =>
        requestAnimationFrame(() => {
          bar.style.transition = `transform ${seconds}s linear`;
          bar.style.transform = "scaleX(0)";
        }),
      );
    }
  }

  private play(event: CameraAlertEvent): void {
    this.current = event;
    const seconds = event.duration_seconds ?? 30;
    this.endsAt = Date.now() + seconds * 1000;

    this.el.dataset.severity = event.severity ?? "alert";
    this.el.style.setProperty(
      "--calert-cols",
      String(Math.min(event.cameras.length, 4)),
    );
    this.el.classList.remove("hidden");
    this.el.classList.add("alert-takeover", "alert-entering");
    this.el.innerHTML = `<div class="alert-flash" aria-hidden="true"></div>
      <div class="calert-banner">
        <span class="calert-icon">${WEATHER_ICONS.warning}</span>
        <span class="calert-title">${esc(event.title)}</span>
        ${event.subtitle ? `<span class="calert-sub">${esc(event.subtitle)}</span>` : ""}
        <span class="calert-count">${seconds}</span>
      </div>
      <div class="calert-wall">${event.cameras
        .slice(0, 4)
        .map(
          (c) => `<div class="calert-tile" data-cam="${esc(c.id)}">
          <img class="calert-feed" alt="">
          <div class="calert-tile-label">${esc(c.label)}</div>
          <div class="calert-tile-state">connecting…</div>
        </div>`,
        )
        .join("")}</div>
      <div class="calert-progress"></div>`;

    // Feeds start now, not after the entrance — otherwise the first second of
    // camera time is spent on a TCP handshake behind an opaque animation.
    for (const tile of this.el.querySelectorAll<HTMLElement>(".calert-tile")) {
      this.startFeed(tile, event.transport === "snapshot");
    }

    this.timers.push(
      window.setTimeout(() => this.el.classList.remove("alert-entering"), ENTRANCE_MS),
    );

    // One CSS transition drives the whole progress bar: no JS per frame.
    const bar = this.el.querySelector<HTMLElement>(".calert-progress")!;
    bar.style.transition = `transform ${seconds}s linear`;
    requestAnimationFrame(() => (bar.style.transform = "scaleX(0)"));

    const count = this.el.querySelector<HTMLElement>(".calert-count")!;
    this.intervals.push(
      window.setInterval(() => {
        count.textContent = String(
          Math.max(0, Math.ceil((this.endsAt - Date.now()) / 1000)),
        );
      }, 250),
    );

    this.timers.push(window.setTimeout(() => this.dismiss(), seconds * 1000));
    this.onOpenChange(true);
  }

  /** Wire one tile: MJPEG first, stills as the fallback, placeholder as the floor. */
  private startFeed(tile: HTMLElement, snapshotOnly: boolean): void {
    const id = tile.dataset.cam!;
    const img = tile.querySelector<HTMLImageElement>("img")!;
    const state = tile.querySelector<HTMLElement>(".calert-tile-state")!;
    let failures = 0;
    let polling = 0;

    const offline = () => {
      this.stopFeed(img);
      if (polling) {
        clearInterval(polling);
        polling = 0;
      }
      tile.classList.add("calert-offline");
      state.textContent = "camera offline";
      state.classList.remove("hidden");
    };

    const toSnapshots = () => {
      if (polling || !tile.isConnected) return;
      tile.dataset.mode = "snapshot";
      state.textContent = "reconnecting…";
      img.onerror = () => {
        if (++failures >= MAX_TILE_FAILURES) offline();
      };
      img.onload = () => {
        failures = 0;
        state.classList.add("hidden");
      };
      const tick = () => {
        img.src = `/api/cameras/${encodeURIComponent(id)}/snapshot?t=${Date.now()}`;
      };
      tick();
      polling = window.setInterval(tick, SNAPSHOT_POLL_MS);
      this.intervals.push(polling);
    };

    if (snapshotOnly) {
      toSnapshots();
      return;
    }

    tile.dataset.mode = "stream";
    img.onload = () => state.classList.add("hidden");
    img.onerror = toSnapshots; // 502/503/404, or the stream closing
    // Cache-busted so a re-fire can't reuse a dead cached connection.
    img.src = `/api/cameras/${encodeURIComponent(id)}/stream?t=${Date.now()}`;
    // Covers the case where the connection opens but no frame ever arrives —
    // `error` never fires there, so the tile would sit on "connecting…".
    this.timers.push(
      window.setTimeout(() => {
        if (tile.dataset.mode === "stream" && !img.naturalWidth) toSnapshots();
      }, STREAM_WATCHDOG_MS),
    );
  }

  /** Kill an MJPEG connection. Pointing src at an inline blank first makes the
   *  decoder release the socket; detaching the node alone does not. */
  private stopFeed(img: HTMLImageElement): void {
    img.onload = null;
    img.onerror = null;
    img.src = BLANK_GIF; // inline data: URI — no network request
    img.removeAttribute("src");
  }

  dismiss(): void {
    if (!this.current) return;
    for (const t of this.timers) clearTimeout(t);
    for (const i of this.intervals) clearInterval(i);
    this.timers = [];
    this.intervals = [];
    for (const img of this.el.querySelectorAll<HTMLImageElement>("img")) {
      this.stopFeed(img);
    }
    this.current = null;
    this.el.classList.add("hidden");
    this.el.classList.remove("alert-takeover", "alert-entering");
    delete this.el.dataset.severity;
    this.el.innerHTML = "";
    this.onOpenChange(false);
    this.drain();
  }

  private drain(): void {
    if (this.current || !this.queue.length) return;
    const next = this.queue.shift()!;
    this.timers.push(window.setTimeout(() => this.show(next), 500));
  }
}
