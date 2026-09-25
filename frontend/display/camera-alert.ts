// Full-screen camera takeover: a theme-aware alert entrance, then a wall of
// live camera tiles, a pulsing banner with a countdown, and auto-dismiss.
// Reusable for any `camera_alert` producer (HA doors, motion, doorbell) —
// everything alert-specific arrives in the event.
//
// Each tile shows one still snapshot at once, then plays WebRTC from Home
// Assistant's go2rtc over it (the camera's own H.264 at 20-30 fps; signalling
// relayed by the backend, backend/webrtc.py). WebRTC can take several seconds
// to its first frame — go2rtc opens the camera stream and waits for a
// keyframe — hence the still. Tiles start a little apart: three cameras'
// first keyframes landing together lost packets on the kiosk's Wi-Fi, and a
// lost keyframe stalls a stream until the next one. If WebRTC fails or shows
// nothing in time, the tile falls back to HA's MJPEG proxy (for UniFi Protect
// a snapshot every 0.5 s — the old 2 fps path) → snapshot polling → offline.
// MJPEG is deliberately not run alongside WebRTC: at up to 15 Mbit/s per tile
// it starved the WebRTC streams.
//
// MJPEG tiles are <img>s holding long-lived HTTP connections. dismiss() MUST
// kill them: detaching the node alone is not reliable in Chromium, and a leaked
// stream permanently burns one of its six per-host connections. See stopFeed().
// WebRTC sessions are closed explicitly too, so HA's go2rtc stops the stream.
//
// All animation is CSS (transform/opacity, the compositor path); the only JS
// that runs per second is the countdown digit.

import { WEATHER_ICONS } from "./icons";
import type { CameraAlertEvent } from "./types";

const ENTRANCE_MS = 900;
const SNAPSHOT_POLL_MS = 1000;
const STREAM_WATCHDOG_MS = 6000; // no first frame by now → fall back to stills
const WEBRTC_WATCHDOG_MS = 12000; // no WebRTC frame by now → MJPEG
const WEBRTC_STAGGER_MS = 400; // between tiles' session starts
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

/** One tile's WebRTC session: the peer connection, and how to end it. */
interface RtcFeed {
  pc: RTCPeerConnection;
  request: string;
}

let rtcSeq = 0;

/** Resolves once ICE gathering is complete (or after 2 s, with whatever is in). */
function gathered(pc: RTCPeerConnection): Promise<void> {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => {
      pc.removeEventListener("icegatheringstatechange", check);
      resolve();
    };
    const check = () => pc.iceGatheringState === "complete" && done();
    pc.addEventListener("icegatheringstatechange", check);
    setTimeout(done, 2000);
  });
}

export class CameraAlertOverlay {
  private queue: CameraAlertEvent[] = [];
  private current: CameraAlertEvent | null = null;
  private timers: number[] = [];
  private intervals: number[] = [];
  private dismissTimer = 0;
  private endsAt = 0;
  private rtcFeeds: RtcFeed[] = [];
  // request id -> handler for the backend's relayed HA WebRTC events
  private rtcHandlers = new Map<string, (event: any) => void>();

  constructor(
    private el: HTMLElement,
    private isBlanked: () => boolean,
    private wakeDisplay: () => void,
    private isPreempted: () => boolean,
    private onOpenChange: (open: boolean) => void,
    // Sends a WebRTC signalling message over the display WebSocket.
    private signal: (message: Record<string, unknown>) => void = () => {},
  ) {
    el.addEventListener("pointerdown", () => this.dismiss());
  }

  /** Debug/test: each live WebRTC tile's connection state. */
  rtcState(): { request: string; connection: string; ice: string }[] {
    return this.rtcFeeds.map((f) => ({
      request: f.request,
      connection: f.pc.connectionState,
      ice: f.pc.iceConnectionState,
    }));
  }

  /** A relayed HA WebRTC event ({type: "webrtc", request, event}). */
  onWebrtc(message: any): void {
    this.rtcHandlers.get(message?.request)?.(message?.event ?? {});
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
    // Only the dismiss clock restarts: clearing every timer here also killed
    // the feeds' no-frame watchdogs, stranding a tile on "connecting…".
    clearTimeout(this.dismissTimer);
    this.dismissTimer = window.setTimeout(() => this.dismiss(), seconds * 1000);
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
          <video class="calert-feed calert-video" muted autoplay playsinline></video>
          <img class="calert-feed" alt="">
          <div class="calert-tile-label">${esc(c.label)}</div>
          <div class="calert-tile-state">connecting…</div>
        </div>`,
        )
        .join("")}</div>
      <div class="calert-progress"></div>`;

    // Feeds start now, not after the entrance — otherwise the first second of
    // camera time is spent on a TCP handshake behind an opaque animation.
    const tiles = [...this.el.querySelectorAll<HTMLElement>(".calert-tile")];
    tiles.forEach((tile, i) => {
      if (event.transport === "snapshot" || typeof RTCPeerConnection === "undefined") {
        this.startFeed(tile, event.transport === "snapshot");
        return;
      }
      this.showStill(tile);
      this.timers.push(
        window.setTimeout(() => this.startWebRtc(tile), i * WEBRTC_STAGGER_MS),
      );
    });

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

    this.dismissTimer = window.setTimeout(() => this.dismiss(), seconds * 1000);
    this.onOpenChange(true);
  }

  /** One snapshot as the tile's picture while live video connects. */
  private showStill(tile: HTMLElement): void {
    const img = tile.querySelector<HTMLImageElement>("img")!;
    const state = tile.querySelector<HTMLElement>(".calert-tile-state")!;
    img.onload = () => state.classList.add("hidden");
    img.onerror = null; // a missing still is fine; the live feed decides the tile
    img.src = `/api/cameras/${encodeURIComponent(tile.dataset.cam!)}/snapshot?t=${Date.now()}`;
  }

  /** Live video over WebRTC; a failure or no frame in time hands the tile to
   *  the MJPEG path. */
  private startWebRtc(tile: HTMLElement): void {
    if (!this.current || !tile.isConnected) return; // dismissed while staggered
    const video = tile.querySelector<HTMLVideoElement>("video")!;
    const state = tile.querySelector<HTMLElement>(".calert-tile-state")!;
    const request = `rtc-${Date.now()}-${++rtcSeq}`;
    // Host candidates only: the appliance and HA share a routed LAN, and a
    // public STUN server would add a dependency for nothing.
    const pc = new RTCPeerConnection({ iceServers: [] });
    const feed: RtcFeed = { pc, request };
    this.rtcFeeds.push(feed);
    let live = false;
    let done = false;

    const fallback = (why: string) => {
      if (done) return;
      done = true;
      console.info(`camera tile: WebRTC → MJPEG (${why})`);
      this.stopRtc(feed);
      video.classList.add("hidden");
      if (tile.isConnected && this.current) this.startFeed(tile, false);
    };

    pc.addTransceiver("video", { direction: "recvonly" });
    pc.ontrack = (e) => {
      if (e.track.kind !== "video") return;
      video.srcObject = e.streams[0] ?? new MediaStream([e.track]);
      video.play().catch(() => undefined); // autoplay covers it; this is belt and braces
    };
    pc.onconnectionstatechange = () => {
      if (pc.connectionState === "failed") fallback("connection failed");
      // A dropped session (backend restart, display reconnect) after video was
      // flowing: the MJPEG path picks the tile back up.
      if (pc.connectionState === "disconnected" && live) {
        this.timers.push(
          window.setTimeout(() => pc.connectionState !== "connected" && fallback("disconnected"), 3000),
        );
      }
    };
    // Revealed on the first *rendered* frame. The element stays laid out
    // (transparent) until then: Chromium neither plays nor paints a
    // display:none video, so hiding it would starve the very check that
    // reveals it.
    const reveal = () => {
      if (done || live) return;
      live = true;
      this.stopFeed(tile.querySelector<HTMLImageElement>("img")!); // the still underneath
      tile.dataset.mode = "webrtc";
      video.classList.add("calert-video-live");
      state.classList.add("hidden");
    };
    if (typeof video.requestVideoFrameCallback === "function") video.requestVideoFrameCallback(reveal);
    else video.addEventListener("playing", reveal, { once: true });

    this.rtcHandlers.set(request, (event) => {
      if (done) return;
      if (event.type === "answer") {
        pc.setRemoteDescription({ type: "answer", sdp: event.answer }).catch(() => fallback("bad answer"));
      } else if (event.type === "candidate" && event.candidate) {
        pc.addIceCandidate(event.candidate).catch(() => undefined); // one bad candidate isn't fatal
      } else if (event.type === "error") {
        fallback(String(event.message ?? "error"));
      }
    });

    // A complete offer (candidates inlined) rather than trickle ICE: with
    // host-only candidates gathering takes milliseconds, and trickling them
    // through two relays made session setup a race that one tile in three lost.
    pc.createOffer()
      .then((offer) => pc.setLocalDescription(offer))
      .then(() => gathered(pc))
      .then(() => {
        if (!done) {
          this.signal({
            type: "webrtc_offer",
            request,
            camera: tile.dataset.cam,
            offer: pc.localDescription!.sdp,
            // Several tiles: the backend may pick a lighter channel per camera.
            wall: (this.current?.cameras.length ?? 1) > 1,
          });
        }
      })
      .catch(() => fallback("offer failed"));

    this.timers.push(window.setTimeout(() => !live && fallback("no video"), WEBRTC_WATCHDOG_MS));
  }

  private stopRtc(feed: RtcFeed): void {
    this.rtcHandlers.delete(feed.request);
    this.rtcFeeds = this.rtcFeeds.filter((f) => f !== feed);
    feed.pc.ontrack = null;
    feed.pc.onicecandidate = null;
    feed.pc.onconnectionstatechange = null;
    feed.pc.close();
    this.signal({ type: "webrtc_close", request: feed.request });
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
    clearTimeout(this.dismissTimer);
    for (const t of this.timers) clearTimeout(t);
    for (const i of this.intervals) clearInterval(i);
    this.timers = [];
    this.intervals = [];
    for (const feed of [...this.rtcFeeds]) this.stopRtc(feed);
    for (const video of this.el.querySelectorAll<HTMLVideoElement>("video")) {
      video.srcObject = null;
    }
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
