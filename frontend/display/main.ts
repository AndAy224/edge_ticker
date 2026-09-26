// Display orchestrator: WS client, rotation engine, zone rendering, gestures.

import "./styles.css";
import { attachGestures } from "./gestures";
import { getRenderer, hasRenderer } from "./modules/registry";
import "./modules/markets";
import "./modules/news";
import { setSportsLiveMode } from "./modules/sports";
import { setFantasyLiveMode } from "./modules/fantasy";
import "./modules/adsb";
import "./modules/astro";
import "./modules/proxmox";
import "./modules/opnsense";
import { setWeatherAlerts } from "./modules/weather";
import "./modules/airquality";
import { setRadarWarnings } from "./modules/weather_radar";
import "./modules/hurricanes";
import "./modules/marine";
import { setLaunchSun } from "./modules/launches";
import { Celebration } from "./celebrate";
import { WeatherAlertOverlay } from "./weather-alert";
import { CameraAlertOverlay } from "./camera-alert";
import { HAOverlay } from "./overlay-ha";
import { Tape } from "./tape";
import type { Config, ModulePayload } from "./types";
import { WEATHER_ICONS, weatherIcon } from "./icons";
import { DEFAULT_LAYOUT, DEFAULT_THEME, LAYOUTS, THEMES } from "../shared/themes";
import { isOutdated, reloadOnto } from "../shared/build";

const stageEl = document.getElementById("stage-content")!;
const dotsEl = document.getElementById("page-dots")!;
const pinBadge = document.getElementById("pin-badge")!;
const railInner = document.getElementById("rail-inner")!;
const clockEl = document.getElementById("clock")!;
const clockChip = document.getElementById("clock-chip")!;
const dateEl = document.getElementById("date")!;
const weatherEl = document.getElementById("weather")!;
const blanker = document.getElementById("blanker")!;
const dimmer = document.getElementById("dimmer")!;
const connDot = document.getElementById("conn-dot")!;

// Night dim is two independent facts: what the scheduler last commanded, and
// whether a full-screen takeover is currently suppressing it. Keeping them
// apart means a `night` message that lands DURING a takeover is recorded and
// applied on restore, instead of being lost or fighting the overlay.
let nightOpacity = "0";
// One flag per takeover kind: a weather card closing must not re-dim while a
// camera takeover it queued behind is still up.
const dimSuppressedBy = new Set<string>();
function applyDim(): void {
  dimmer.style.opacity = dimSuppressedBy.size ? "0" : nightOpacity;
}
function suppressDim(kind: string, open: boolean): void {
  if (open) dimSuppressedBy.add(kind);
  else dimSuppressedBy.delete(kind);
  applyDim();
}
function applyNight(night: any): void {
  // Only the software path dims here; with working DDC the panel itself dims.
  const dim = night?.mode === "dim" && night?.software !== false;
  nightOpacity = dim ? String(1 - (night.level ?? 10) / 100) : "0";
  applyDim();
}

const modules = new Map<string, ModulePayload>();
const haStates = new Map<string, any>(); // alert entities (and mapped) by id
let config: Config = {};
let blanked = false;
const scoreChip = document.getElementById("score-chip")!;

// Multi-pane stage: layouts can show a window of 1–3 consecutive rotation
// modules side by side. Pane i shows rotation.order[(index + i) % length].
let paneEls: HTMLElement[] = [];
const paneDetailTimers = new Map<number, number>(); // pane index -> auto-close timer

// What each pane is currently showing: the module id, or `id#detailKey` while a
// detail layer is up. renderPane() runs on every payload for a visible module,
// not just on a rotation step, so this is what separates a real change of view
// (worth animating) from a routine data refresh (which must stay quiet).
let paneView: (string | undefined)[] = [];
const paneLabelTimers = new Map<number, number>(); // pane index -> deferred label

// Cached from CSS custom properties in applyAppearance(); see the :root block.
let swapExitMs = 320;
let swapLabelMs = 0;

function layoutPanes(): number {
  const layout = config.appearance?.layout ?? DEFAULT_LAYOUT;
  return (LAYOUTS[layout] ?? LAYOUTS[DEFAULT_LAYOUT]).panes;
}

function effectivePaneCount(): number {
  return Math.max(1, Math.min(layoutPanes(), rotation.order.length || 1));
}

function paneModule(i: number): string | undefined {
  if (!rotation.order.length) return undefined;
  return rotation.order[(rotation.index + i) % rotation.order.length];
}

function anyDetailOpen(): boolean {
  return paneDetailTimers.size > 0;
}

const tape = new Tape(document.getElementById("tape-track")!);
const celebration = new Celebration(
  document.getElementById("celebration")!,
  () => blanked,
);
// Debug/test hook: lets devtools (or CDP) fire arbitrary celebration events.
(window as any).__celebrate = (event: any) => celebration.show(event);
const weatherAlert = new WeatherAlertOverlay(
  document.getElementById("weather-alert")!,
  () => blanked,
  () => wake(),
  // A severe-weather card at 3am must be readable: lift the software dim
  // (the backend boosts a DDC dim for it too).
  (open) => suppressDim("weather", open),
);
// Debug/test hook: fire an arbitrary severe-weather alert overlay.
(window as any).__weatheralert = (alert: any) => weatherAlert.show(alert);
const cameraAlert = new CameraAlertOverlay(
  document.getElementById("camera-alert")!,
  () => blanked,
  () => wake(),
  () => weatherAlert.isOpen(), // severe weather is life-safety; it outranks a door
  (open) => {
    suppressDim("camera", open);
    reportDisplayState();
  },
  (message) => sendWs(message), // WebRTC signalling, relayed to HA by the backend
);
// Debug/test hook: fire an arbitrary camera takeover.
(window as any).__cameraalert = (event: any) => cameraAlert.show(event);
// Debug/test hook: WebRTC session state of the takeover's tiles.
(window as any).__camrtc = () => cameraAlert.rtcState();

/** Any full-screen takeover is up. Rotation, auto-feature and gestures all
 *  defer to this — previously only the HA swipe-up overlay did, so the stage
 *  quietly rotated and crossfaded behind every celebration. */
function takeoverOpen(): boolean {
  return cameraAlert.isOpen() || celebration.isOpen() || weatherAlert.isOpen();
}

const overlay = new HAOverlay(
  document.getElementById("overlay")!,
  (domain, service, entityId, data) =>
    sendWs({ type: "ha_action", domain, service, entity_id: entityId, data }),
);

// ---- WebSocket -------------------------------------------------------------

let ws: WebSocket | null = null;
let reconnectDelay = 1000;
let lastMessageAt = Date.now();
let disconnectedSince = 0;

function sendWs(message: Record<string, unknown>): void {
  if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify(message));
}

function connect(): void {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws/display`);
  ws.onopen = () => {
    reconnectDelay = 1000;
    disconnectedSince = 0;
    lastMessageAt = Date.now();
    connDot.classList.add("hidden");
    reportDisplayState();
  };
  ws.onclose = () => {
    connDot.classList.remove("hidden");
    if (!disconnectedSince) disconnectedSince = Date.now();
    setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 15_000);
  };
  ws.onmessage = (event) => {
    lastMessageAt = Date.now();
    handleMessage(JSON.parse(event.data));
  };
}

// Connection watchdog: a socket that's open but silent past the heartbeat
// window is dead — force-close it to trigger the reconnect path. If we can't
// reconnect for 10 minutes, hard-reload the page (Chromium self-heal).
setInterval(() => {
  if (ws?.readyState === WebSocket.OPEN && Date.now() - lastMessageAt > 45_000) {
    ws.close();
  }
  if (disconnectedSince && Date.now() - disconnectedSince > 600_000) {
    selfHealReload();
  }
}, 15_000);

/** Reload only once the backend answers: reloading into a dead server leaves
 *  Chromium on its own error page with no script left to retry, and the panel
 *  stays there until the watchdog restarts the kiosk. */
let selfHealing = false;
async function selfHealReload(): Promise<void> {
  if (selfHealing) return;
  selfHealing = true;
  try {
    const r = await fetch("/api/health", { cache: "no-store" });
    if (r.ok) location.reload();
  } catch {
    // still down — the next watchdog tick tries again
  } finally {
    selfHealing = false;
  }
}

function reportDisplayState(): void {
  sendWs({
    type: "display_state",
    state: {
      module: rotation.current() ?? null,
      pinned: rotation.pinned,
      blanked,
      overlay: overlay.isOpen(),
      takeover: cameraAlert.isOpen(),
    },
  });
}

function handleMessage(msg: any): void {
  switch (msg.type) {
    case "snapshot":
      // Every (re)connect after a deploy lands here: move onto the new bundle
      // rather than run old JS against the new backend.
      if (isOutdated(msg.build?.display) && reloadOnto(msg.build.display)) return;
      modules.clear();
      for (const [name, payload] of Object.entries(msg.modules ?? {})) {
        modules.set(name, payload as ModulePayload);
      }
      config = msg.config ?? {};
      overlay.setMapping(config.ha);
      overlay.setStates(msg.ha?.states ?? {}, msg.ha?.status);
      overlay.setSystem(msg.system?.ip ?? null);
      haStates.clear();
      for (const [id, s] of Object.entries(msg.ha?.states ?? {})) haStates.set(id, s);
      setWeatherAlerts((modules.get("weather_alerts")?.stage as any)?.alerts ?? []);
      setRadarWarnings((modules.get("weather_alerts")?.stage as any)?.nearby ?? []);
      setLaunchSun((modules.get("weather")?.stage as any)?.sun ?? null);
      if (msg.night) applyNight(msg.night);
      applyConfig();
      renderWeather();
      // Re-evaluate auto-feature from the snapshot so a mid-game reload
      // (nightly reload, self-heal) features immediately instead of waiting
      // for the next sports poll. `liveFeatured` is in-memory, so a reload
      // after a manual unpin mid-game re-features once — accepted.
      {
        const sports = modules.get("sports");
        if (sports) autoFeatureSports(sports);
        const fantasy = modules.get("fantasy");
        if (fantasy) autoFeatureFantasy(fantasy);
        const launches = modules.get("launches");
        if (launches) autoFeatureLaunches(launches);
        const hurricanes = modules.get("hurricanes");
        if (hurricanes) autoFeatureHurricanes(hurricanes);
        const network = modules.get("opnsense");
        if (network) autoFeatureNetwork(network);
      }
      updateScoreChip();
      break;
    case "module": {
      const payload: ModulePayload = msg.payload;
      modules.set(payload.module, payload);
      if (payload.module === "weather") {
        renderWeather();
        setLaunchSun((payload.stage as any)?.sun ?? null);
      }
      if (payload.module === "weather_alerts") {
        // Alerts render inside the weather rail/stage and outline on the
        // radar — not in a pane of their own.
        setWeatherAlerts(payload.stage?.alerts ?? []);
        setRadarWarnings(payload.stage?.nearby ?? []);
        renderWeather();
        for (let i = 0; i < paneEls.length; i++) {
          const id = paneModule(i);
          if ((id === "weather" || id === "weather_radar") && !paneDetailTimers.has(i)) renderPane(i);
        }
      }
      if (payload.module === "sports") {
        autoFeatureSports(payload);
        updateScoreChip();
      }
      if (payload.module === "fantasy") autoFeatureFantasy(payload);
      if (payload.module === "launches") autoFeatureLaunches(payload);
      if (payload.module === "hurricanes") autoFeatureHurricanes(payload);
      if (payload.module === "opnsense") autoFeatureNetwork(payload);
      if (blanked) {
        // Nobody can see it: keep the data, skip the DOM work until wake().
        renderDeferred = true;
        break;
      }
      rebuildTape();
      for (let i = 0; i < paneEls.length; i++) {
        if (paneModule(i) === payload.module && !paneDetailTimers.has(i)) {
          renderPane(i);
        }
      }
      break;
    }
    case "module_removed":
      // The collector stopped (module disabled): drop its data rather than
      // leave e.g. an expired warning on the tape until the next reload.
      modules.delete(msg.module);
      if (msg.module === "weather_alerts") {
        setWeatherAlerts([]);
        setRadarWarnings([]);
      }
      renderWeather();
      rebuildTape();
      renderStage();
      break;
    case "config":
      config = msg.config ?? {};
      overlay.setMapping(config.ha);
      applyConfig();
      break;
    case "control":
      handleControl(msg.action);
      break;
    case "sport_event":
      if (config.modules?.sports?.celebrations !== false) celebration.show(msg.event);
      break;
    case "fantasy_event":
      if ((config.modules?.fantasy as any)?.celebrations !== false) celebration.show(msg.event);
      break;
    case "weather_alert":
      weatherAlert.show(msg.alert);
      break;
    case "webrtc":
      cameraAlert.onWebrtc(msg);
      break;
    case "camera_alert":
      // A takeover wins over a celebration (z-index 92 vs 80), so end that one
      // rather than leaving its state machine ticking behind an opaque wall.
      if (celebration.isOpen()) celebration.dismiss();
      cameraAlert.show(msg.event);
      break;
    case "ha_state": {
      overlay.updateState(msg.entity_id, { state: msg.state, attributes: msg.attributes });
      const previous = haStates.get(msg.entity_id)?.state;
      haStates.set(msg.entity_id, { state: msg.state, attributes: msg.attributes });
      haTransitionToast(msg.entity_id, previous, msg.state);
      rebuildTape(); // alert items may have changed
      break;
    }
    case "ha_states":
      overlay.setStates(msg.states ?? {}, msg.status);
      haStates.clear();
      for (const [id, s] of Object.entries(msg.states ?? {})) haStates.set(id, s);
      rebuildTape();
      break;
    case "ha_status":
      overlay.setStatus(msg.status);
      break;
    case "night":
      // `software` says whether the dim is ours to draw (DDC unavailable) —
      // false clears an overlay left from an earlier DDC failure.
      applyNight({ ...msg, software: msg.software !== false });
      break;
  }
}

setInterval(() => sendWs({ type: "ping" }), 10_000);

// ---- Rotation engine ---------------------------------------------------------

const rotation = {
  order: [] as string[],
  index: 0,
  pinned: false,
  timer: 0,

  current(): string | undefined {
    return this.order[this.index];
  },
  schedule(): void {
    clearInterval(this.timer);
    // Floored: a 0 here (a cleared admin field) rotated as fast as setInterval allows.
    const seconds = Math.max(5, Number(config.rotation?.interval_seconds) || 25);
    this.timer = window.setInterval(() => {
      if (
        !this.pinned &&
        !overlay.isOpen() &&
        !blanked &&
        !anyDetailOpen() &&
        !takeoverOpen()
      ) {
        this.next();
      }
    }, seconds * 1000);
  },
  next(): void {
    // No-op when every module is already visible; re-evaluated per tick so
    // rotation resumes if a config change grows the order past the pane count.
    if (this.order.length <= effectivePaneCount()) return;
    this.index = (this.index + 1) % this.order.length;
    closeAllDetails();
    renderStage();
    reportDisplayState();
  },
  prev(): void {
    if (this.order.length <= effectivePaneCount()) return;
    this.index = (this.index - 1 + this.order.length) % this.order.length;
    closeAllDetails();
    renderStage();
    reportDisplayState();
  },
  togglePin(): void {
    this.pinned = !this.pinned;
    autoPinnedFor = null; // manual pin/unpin takes ownership from auto-feature
    pinBadge.classList.toggle("hidden", !this.pinned);
    reportDisplayState();
  },
};

function applyConfig(): void {
  applyAppearance();
  setSportsLiveMode((config.modules?.sports as any)?.live_mode !== false);
  setFantasyLiveMode((config.modules?.fantasy as any)?.live_mode !== false);
  const featureOff =
    (autoPinnedFor === "sports" && config.modules?.sports?.auto_feature !== true) ||
    (autoPinnedFor === "fantasy" && (config.modules?.fantasy as any)?.auto_feature === false) ||
    (autoPinnedFor === "hurricanes" && (config.modules?.hurricanes as any)?.auto_feature !== true) ||
    (autoPinnedFor === "opnsense" && (config.modules?.opnsense as any)?.auto_feature === false);
  if (featureOff) {
    // Feature toggled off mid-game: release our pin, keep a manual one.
    autoPinnedFor = null;
    rotation.pinned = false;
    pinBadge.classList.add("hidden");
    reportDisplayState();
  }
  rotation.order = (config.rotation?.order ?? []).filter(
    (id) => hasRenderer(id) && config.modules?.[id]?.enabled !== false,
  );
  if (rotation.index >= rotation.order.length) rotation.index = 0;
  closeAllDetails(); // pane→module mapping may have changed under open details
  syncPanes();
  rotation.schedule();
  rebuildTape();
  renderStage();
}

/** (Re)build the .stage-pane containers when the effective count changes. */
function syncPanes(): void {
  const count = effectivePaneCount();
  document.documentElement.dataset.panes = String(count);
  if (paneEls.length === count) return;
  // Resize rather than clear: growing 2→3 panes leaves panes 0-1 on the same
  // modules, and clearing would make them all read as changed and wipe.
  paneView.length = count;
  stageEl.replaceChildren();
  paneEls = Array.from({ length: count }, (_, i) => {
    const pane = document.createElement("div");
    pane.className = "stage-pane";
    pane.dataset.pane = String(i);
    const header = document.createElement("div");
    header.className = "pane-header"; // hidden by default; themes opt in
    pane.appendChild(header);
    stageEl.appendChild(pane);
    return pane;
  });
}

// Small-caps pane labels (glance theme). Fallback: uppercase the module id.
const MODULE_LABELS: Record<string, string> = {
  adsb: "OVERHEAD",
  airquality: "AIR QUALITY",
  fantasy: "FANTASY",
  weather_radar: "RADAR",
  hurricanes: "TROPICS",
  marine: "TIDES",
  launches: "LAUNCHES",
  opnsense: "NETWORK",
};

function paneLabel(id: string | undefined): string {
  if (!id) return "";
  return MODULE_LABELS[id] ?? id.toUpperCase();
}

function applyAppearance(): void {
  const root = document.documentElement;
  const themeId = config.appearance?.theme ?? DEFAULT_THEME;
  const theme = THEMES[themeId] ?? THEMES[DEFAULT_THEME];
  root.dataset.theme = themeId in THEMES ? themeId : DEFAULT_THEME;
  for (const [name, value] of Object.entries(theme.vars)) {
    root.style.setProperty(name, value);
  }
  const layout = config.appearance?.layout ?? DEFAULT_LAYOUT;
  root.dataset.layout = layout in LAYOUTS ? layout : DEFAULT_LAYOUT;
  // Stage-swap timings come from CSS, not from THEMES: applyAppearance writes
  // theme vars inline on <html> and never clears the outgoing theme's, so a
  // thirteenth key in themes.ts would leak into every theme picked afterwards.
  // Read after the attribute is set — getComputedStyle forces the recalc, so
  // the new theme's block is already in effect. Once per config apply.
  const cs = getComputedStyle(root);
  swapExitMs = parseFloat(cs.getPropertyValue("--stage-exit-ms")) || 320;
  swapLabelMs = parseFloat(cs.getPropertyValue("--stage-label-ms")) || 0;
}

// ---- Stage -------------------------------------------------------------------

function renderStage(): void {
  renderDots();
  for (let i = 0; i < paneEls.length; i++) {
    if (!paneDetailTimers.has(i)) renderPane(i);
  }
  updateScoreChip(); // visibility depends on whether sports is on screen
}

function renderPane(i: number): void {
  const id = paneModule(i);
  const layer = document.createElement("div");
  layer.className = "stage-layer";
  if (!id) {
    layer.innerHTML = `<div class="empty">No modules enabled</div>`;
  } else {
    const payload = modules.get(id);
    const renderer = getRenderer(id);
    if (!payload || !renderer) {
      layer.innerHTML = `<div class="empty">Waiting for ${paneLabel(id).toLowerCase()} data…</div>`;
    } else {
      try {
        renderer.renderStage(layer, payload.stage);
      } catch (err) {
        // Contained per pane: an unexpected payload shape in one module must
        // not take the other panes, the score chip and the state report with it.
        console.error(`renderStage(${id}) failed`, err);
        layer.innerHTML = `<div class="empty">${paneLabel(id)} couldn't be drawn</div>`;
      }
      if (payload.stale) {
        const dot = document.createElement("span");
        dot.className = "stale-dot";
        dot.title = "data is stale";
        layer.appendChild(dot);
      }
    }
  }
  crossfade(i, layer, id, paneLabel(id));
}

/**
 * Swap a pane's contents. `view` identifies what the pane is about to show (the
 * module id, or `id#detailKey` for a detail layer); when it differs from what
 * the pane was showing this is a real change and themes may animate it, and
 * when it matches this is a routine data refresh and stays quiet.
 */
function crossfade(
  pane: number,
  layer: HTMLElement,
  view: string | undefined,
  label: string,
): void {
  const container = paneEls[pane];
  // Only layers are replaced — panes also hold a persistent .pane-header.
  const previous = Array.from(container.querySelectorAll(":scope > .stage-layer"));
  // A pane with nothing in it yet isn't changing views, it's arriving: at boot
  // and after every pane rebuild there is no outgoing layer to wipe away.
  const swap = previous.length > 0 && paneView[pane] !== view;
  paneView[pane] = view;

  // A routine data refresh of the view already on screen: swap in one frame,
  // no crossfade. Crossfading every refresh (markets streams every ~2s) put a
  // translucent ghost of the old digits over the new ones for 300ms.
  const current = previous.filter((el) => !el.classList.contains("exit"));
  const midSwap = current.some((el) => el.classList.contains("swap-in"));
  if (!swap && current.length && !midSwap) {
    quietRefresh(pane, container, layer, current);
    const header = container.querySelector<HTMLElement>(".pane-header");
    if (header && !paneLabelTimers.has(pane)) header.textContent = label;
    return;
  }
  pendingRefresh.get(pane)?.remove(); // a view change supersedes a queued refresh
  pendingRefresh.delete(pane);

  // swap-in/swap-out are additive markers on top of the existing enter/exit, so
  // a theme that ignores them behaves exactly as it did before.
  layer.classList.add("enter");
  if (swap) layer.classList.add("swap-in");
  container.appendChild(layer);
  // Deliberately no style read between the append and this rAF: `.enter` is
  // never observed in computed style, so no theme fades the incoming layer in.
  requestAnimationFrame(() => layer.classList.remove("enter"));
  for (const el of previous) {
    el.classList.add("exit");
    if (swap) el.classList.add("swap-out");
    setTimeout(() => el.remove(), swap ? swapExitMs : 320);
  }

  // Drop the marker once the animation is done, so the sweep pseudo-element
  // doesn't linger on a layer that stays on screen for the next 25 seconds.
  if (swap) setTimeout(() => layer.classList.remove("swap-in"), swapExitMs);

  const header = container.querySelector<HTMLElement>(".pane-header");
  clearTimeout(paneLabelTimers.get(pane));
  paneLabelTimers.delete(pane);
  if (!header) return;
  if (!swap || swapLabelMs <= 0) {
    header.textContent = label; // themes that don't animate the chrome
    return;
  }
  // The pane header is persistent, so its animation needs an explicit restart.
  // The forced reflow is nearly free here — renderPane just replaced the pane's
  // subtree, so layout is already dirty and this only pulls the cost forward.
  container.classList.remove("swapping");
  void container.offsetWidth;
  container.classList.add("swapping");
  // Land the new label while the chrome is mid-animation and out of sight.
  paneLabelTimers.set(
    pane,
    window.setTimeout(() => {
      paneLabelTimers.delete(pane);
      header.textContent = label;
    }, swapLabelMs),
  );
}

// Per pane: a refreshed layer waiting for its images to decode.
const pendingRefresh = new Map<number, HTMLElement>();

/** Replace `current` with `layer` once the new layer's images are decoded, so
 *  the swap can't flash an empty tile mosaic or logo slot. The new layer sits
 *  hidden (but laid out — map renderers measure themselves after attach) until
 *  then; a newer refresh for the same pane supersedes it. */
function quietRefresh(
  pane: number,
  container: HTMLElement,
  layer: HTMLElement,
  current: Element[],
): void {
  pendingRefresh.get(pane)?.remove();
  layer.classList.add("pending");
  container.appendChild(layer);
  pendingRefresh.set(pane, layer);
  // Map renderers build their tiles in a requestAnimationFrame after attach;
  // this frame callback is queued after theirs, so their images exist by now.
  requestAnimationFrame(() => {
    const images = Array.from(layer.querySelectorAll("img"));
    const decoded = Promise.all(images.map((img) => img.decode().catch(() => undefined)));
    const timeout = new Promise((resolve) => setTimeout(resolve, 1500));
    Promise.race([decoded, timeout]).then(() => {
      if (pendingRefresh.get(pane) !== layer || !layer.isConnected) return; // superseded
      pendingRefresh.delete(pane);
      for (const el of current) el.remove();
      layer.classList.remove("pending");
    });
  });
}

function renderDots(): void {
  // Hidden when rotation can't advance (everything is already visible).
  dotsEl.classList.toggle("hidden", rotation.order.length <= effectivePaneCount());
  dotsEl.innerHTML = rotation.order
    .map((_, i) => `<span class="dot ${i === rotation.index ? "active" : ""}"></span>`)
    .join("");
}

// ---- Tap-to-expand detail ------------------------------------------------------

function handleStageTap(target: EventTarget | null): void {
  const paneEl = (target as HTMLElement)?.closest?.<HTMLElement>(".stage-pane");
  if (!paneEl) return;
  const pane = Number(paneEl.dataset.pane);
  const key = (target as HTMLElement)?.closest?.<HTMLElement>("[data-detail]")?.dataset
    .detail;
  const open = paneDetailTimers.has(pane);
  // A tap inside an open detail that doesn't hit a [data-detail] target closes it.
  if (open && key == null) {
    closeDetail(pane);
    renderPane(pane);
    return;
  }
  if (key == null) return;
  const id = paneModule(pane);
  if (!id) return;
  const renderer = getRenderer(id);
  const payload = modules.get(id);
  if (!renderer?.renderDetail || !renderer.getDetailItem || !payload) return;
  const item = renderer.getDetailItem(payload.stage, key);
  if (!item) {
    if (open) {
      closeDetail(pane);
      renderPane(pane);
    }
    return;
  }
  // Render (or, if a detail is already open, drill into) the detail layer.
  const layer = document.createElement("div");
  layer.className = "stage-layer";
  renderer.renderDetail(layer, item);
  // Keyed by the detail item, so drilling from one detail into another counts
  // as a change of view too. Both close paths route back through renderPane(),
  // whose key is the bare module id, so returning also reads as a change.
  crossfade(pane, layer, `${id}#${key}`, paneLabel(id));
  clearTimeout(paneDetailTimers.get(pane));
  paneDetailTimers.set(
    pane,
    window.setTimeout(() => {
      closeDetail(pane);
      renderPane(pane);
    }, 20_000),
  );
}

function closeDetail(pane: number): void {
  clearTimeout(paneDetailTimers.get(pane));
  paneDetailTimers.delete(pane);
}

function closeAllDetails(): void {
  for (const pane of [...paneDetailTimers.keys()]) closeDetail(pane);
}

// ---- Tape ----------------------------------------------------------------------

// ---- Live-game auto-pin ----------------------------------------------------------
// When a followed team's game goes live (and the toggle is on), jump to sports
// and pin. Edge-triggered: a manual unpin or swipe-away isn't fought until the
// next game starts; when no followed game is live anymore, auto-unpin.
// `autoPinned` marks a pin *we* created — a user's manual pin is never removed.
let liveFeatured = false; // sports: a followed game is live (edge tracker)
let fantasyLiveFeatured = false; // fantasy: my matchup is live (edge tracker)
let autoPinnedFor: string | null = null; // module id we auto-pinned for (null = none)

/** Pin to `id` on the not-live→live edge (only if its feature is enabled). */
function applyAutoFeature(id: string, enabled: boolean): void {
  if (
    enabled &&
    !blanked &&
    !takeoverOpen() && // don't yank+pin the stage under a full-screen takeover
    !overlay.isOpen() &&
    !anyDetailOpen() &&
    !rotation.pinned
  ) {
    const target = rotation.order.indexOf(id);
    if (target >= 0) {
      // Multi-pane: if the module is already on screen, pin in place without a
      // gratuitous full-stage crossfade.
      if (!paneEls.some((_, i) => paneModule(i) === id)) {
        rotation.index = target;
        renderStage();
      }
      rotation.pinned = true;
      autoPinnedFor = id;
      pinBadge.classList.remove("hidden");
      reportDisplayState();
    }
  }
}

/** Release the auto-pin on the live→not-live edge (manual pins are untouched). */
function clearAutoFeature(id: string): void {
  if (autoPinnedFor === id) {
    autoPinnedFor = null;
    rotation.pinned = false;
    pinBadge.classList.add("hidden");
    reportDisplayState();
  }
}

function autoFeatureSports(payload: ModulePayload): void {
  const games: any[] = payload.stage?.games ?? [];
  const liveFollowed = games.some((g) => g.followed && g.state === "in");
  if (liveFollowed && !liveFeatured) {
    liveFeatured = true;
    applyAutoFeature("sports", config.modules?.sports?.auto_feature === true);
  } else if (!liveFollowed && liveFeatured) {
    liveFeatured = false;
    clearAutoFeature("sports");
  }
}

let launchLiveFeatured = false;
function autoFeatureLaunches(payload: ModulePayload): void {
  const live = (payload.stage as any)?.live === true;
  if (live && !launchLiveFeatured) {
    launchLiveFeatured = true;
    applyAutoFeature("launches", (config.modules?.launches as any)?.auto_feature === true);
  } else if (!live && launchLiveFeatured) {
    launchLiveFeatured = false;
    clearAutoFeature("launches");
  }
}

let hurricaneThreatFeatured = false;
/** Home inside a forecast cone: pin the tropics page (opt-in). */
function autoFeatureHurricanes(payload: ModulePayload): void {
  const threat = Boolean((payload.stage as any)?.threat);
  if (threat && !hurricaneThreatFeatured) {
    hurricaneThreatFeatured = true;
    applyAutoFeature("hurricanes", (config.modules?.hurricanes as any)?.auto_feature === true);
  } else if (!threat && hurricaneThreatFeatured) {
    hurricaneThreatFeatured = false;
    clearAutoFeature("hurricanes");
  }
}

let networkFailoverFeatured = false;
/** Running on the backup WAN: pin the network page until the primary is back
 *  (on unless modules.opnsense.auto_feature is false). */
function autoFeatureNetwork(payload: ModulePayload): void {
  const failover = Boolean((payload.stage as any)?.failover);
  if (failover && !networkFailoverFeatured) {
    networkFailoverFeatured = true;
    applyAutoFeature("opnsense", (config.modules?.opnsense as any)?.auto_feature !== false);
  } else if (!failover && networkFailoverFeatured) {
    networkFailoverFeatured = false;
    clearAutoFeature("opnsense");
  }
}

function autoFeatureFantasy(payload: ModulePayload): void {
  const live = (payload.stage as any)?.matchup?.state === "in";
  if (live && !fantasyLiveFeatured) {
    fantasyLiveFeatured = true;
    applyAutoFeature("fantasy", (config.modules?.fantasy as any)?.auto_feature !== false);
  } else if (!live && fantasyLiveFeatured) {
    fantasyLiveFeatured = false;
    clearAutoFeature("fantasy");
  }
}

/** A manual swipe while auto-pinned releases the pin (but not `liveFeatured`,
 *  so the display won't yank back until the next game-start edge). */
function releaseAutoPin(): void {
  if (!autoPinnedFor) return;
  autoPinnedFor = null;
  rotation.pinned = false;
  pinBadge.classList.add("hidden");
}

// ---- HA transition toast --------------------------------------------------------
// Small banner that pops up live when a configured alert entity changes state
// (door just opened / just closed). The tape item covers the steady state;
// this covers the moment.

const toastEl = document.getElementById("toast")!;
let toastTimer = 0;

const STATE_VERBS: Record<string, string> = {
  on: "opened",
  off: "closed",
  open: "opened",
  closed: "closed",
  unlocked: "unlocked",
  locked: "locked",
};

function showToast(text: string, isAlert: boolean): void {
  if (blanked) return;
  clearTimeout(toastTimer);
  toastEl.className = `toast-show ${isAlert ? "toast-alert" : ""}`;
  toastEl.textContent = text;
  toastTimer = window.setTimeout(() => {
    toastEl.classList.remove("toast-show");
    toastTimer = window.setTimeout(() => toastEl.classList.add("hidden"), 400);
  }, 8000);
}

function haTransitionToast(entityId: string, previous: string | undefined, state: string): void {
  if (previous === undefined || previous === state) return; // first sighting / no change
  const alert = (config.ha?.alerts ?? []).find((a) => a?.entity === entityId);
  if (!alert) return;
  const entering = state === alert.state;
  const name =
    haStates.get(entityId)?.attributes?.friendly_name ?? entityId.split(".")[1] ?? entityId;
  const verb = STATE_VERBS[state] ?? state;
  const text = entering && alert.text ? alert.text : `${name} ${verb}`;
  showToast(text, entering);
}

// Debug/test hook: simulate an HA state transition.
(window as any).__hatest = (entityId: string, state: string, name?: string) => {
  const previous = haStates.get(entityId)?.state ?? (state === "on" ? "off" : "on");
  haStates.set(entityId, { state, attributes: { friendly_name: name ?? entityId } });
  haTransitionToast(entityId, previous, state);
  rebuildTape();
};

// Debug/test hook: drive the swipe-up overlay without a live Home Assistant —
// replaces its state map (mapping still comes from config) and opens it.
(window as any).__hafake = (states: Record<string, any>, status = "connected") => {
  overlay.setStates(states, status);
  overlay.open();
};

function haAlertItems(): { text: string; accent: "alert"; priority: number }[] {
  const items: { text: string; accent: "alert"; priority: number }[] = [];
  for (const alert of config.ha?.alerts ?? []) {
    if (!alert?.entity || !alert?.state) continue;
    const s = haStates.get(alert.entity);
    if (s && s.state === alert.state) {
      items.push({
        text:
          alert.text ||
          `${s.attributes?.friendly_name ?? alert.entity} ${s.state}`,
        accent: "alert" as const,
        priority: 2,
      });
    }
  }
  return items;
}

function rebuildTape(): void {
  // Set-dedupe: weather (and weather alerts, which never rotate) always
  // contribute, but only once each if present in the rotation.
  const order = [...new Set([...rotation.order, "weather", "weather_alerts"])];
  const items = [
    ...haAlertItems(),
    ...order.flatMap((id) =>
      [...(modules.get(id)?.tape ?? [])].sort((a, b) => b.priority - a.priority),
    ),
  ];
  tape.setItems(items);
}

// ---- Live score chip -----------------------------------------------------------
// Persistent mini scoreboard while a followed team's game is live, hidden when
// the sports module is already on screen.

function updateScoreChip(): void {
  const games: any[] = (modules.get("sports")?.stage as any)?.games ?? [];
  const live = games.find((g) => g.followed && g.state === "in");
  const sportsVisible = paneEls.some((_, i) => paneModule(i) === "sports");
  if (!live || sportsVisible || blanked) {
    scoreChip.classList.add("hidden");
    return;
  }
  scoreChip.innerHTML = `
    ${live.away?.logo ? `<img src="${live.away.logo}" alt="">` : ""}
    <span class="sc-team">${live.away?.abbrev ?? ""}</span>
    <span class="sc-score">${live.away?.score ?? ""}</span>
    <span class="sc-dash">–</span>
    <span class="sc-score">${live.home?.score ?? ""}</span>
    <span class="sc-team">${live.home?.abbrev ?? ""}</span>
    ${live.home?.logo ? `<img src="${live.home.logo}" alt="">` : ""}
    <span class="sc-detail">${live.detail ?? ""}</span>`;
  scoreChip.classList.remove("hidden");
}

// Debug/test hook: replace the sports payload entirely and re-render,
// running the same side-effects as a real sports module message.
(window as any).__sportsfake = (games: any[]) => {
  const payload = { module: "sports", stage: { games }, tape: [] } as any;
  modules.set("sports", payload);
  autoFeatureSports(payload);
  rebuildTape();
  renderStage();
  updateScoreChip();
};

// Debug/test hook: replace the adsb payload entirely and re-render (for
// screenshotting the radar when the sky overhead is quiet).
(window as any).__adsbfake = (stage: any) => {
  const payload = { module: "adsb", stage, tape: [] } as any;
  modules.set("adsb", payload);
  rebuildTape();
  renderStage();
};

// Debug/test hook: jump rotation so `id` is in pane 0 and pin there (for
// screenshotting a specific module without waiting out the rotation).
(window as any).__rotshow = (id: string) => {
  const target = rotation.order.indexOf(id);
  if (target === -1) return null;
  rotation.index = target;
  rotation.pinned = true;
  closeAllDetails();
  renderStage();
  return rotation.order[target];
};

// Debug/test hook: replace any module's payload and re-render (for
// screenshotting stress payloads). Modules with auto-feature side effects
// (sports, fantasy) have dedicated hooks below.
(window as any).__modfake = (id: string, stage: any) => {
  modules.set(id, { module: id, stage, tape: [] } as any);
  rebuildTape();
  renderStage();
};

// Debug/test hook: replace the fantasy payload entirely and re-render,
// running the same auto-feature side-effects as a real fantasy message.
(window as any).__fantasyfake = (stage: any) => {
  const payload = { module: "fantasy", stage, tape: [] } as any;
  modules.set("fantasy", payload);
  autoFeatureFantasy(payload);
  rebuildTape();
  renderStage();
};

// Debug/test hook: replace the launches payload entirely and re-render,
// running the same auto-feature side-effects as a real launches message.
(window as any).__launchfake = (stage: any) => {
  const payload = { module: "launches", stage, tape: [] } as any;
  modules.set("launches", payload);
  autoFeatureLaunches(payload);
  rebuildTape();
  renderStage();
};

// Debug/test hook: replace the network payload and run its failover auto-pin.
(window as any).__netfake = (stage: any) => {
  const payload = { module: "opnsense", stage, tape: [] } as any;
  modules.set("opnsense", payload);
  autoFeatureNetwork(payload);
  rebuildTape();
  renderStage();
};

// Debug/test hook: fire a fantasy scoring celebration.
(window as any).__fantasyevent = (event: any) => celebration.show(event);

// Debug/test hook: inject a fabricated live game and refresh the chip.
(window as any).__scorechip = (game: any) => {
  const payload: any = modules.get("sports") ?? { module: "sports", stage: {}, tape: [] };
  payload.stage = { games: [game] };
  modules.set("sports", payload);
  updateScoreChip();
};

// ---- Status rail -----------------------------------------------------------------

let clockText = "";
function tickClock(): void {
  const now = new Date();
  const time = now.toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
  });
  if (time === clockText) return; // ticks every second, changes once a minute
  clockText = time;
  clockEl.textContent = time;
  clockChip.textContent = time; // rail-less layouts (focus/mosaic) show the chip
  dateEl.textContent = now.toLocaleDateString([], {
    weekday: "long",
    month: "long",
    day: "numeric",
  });
}
tickClock();
setInterval(tickClock, 1000);

// Burn-in mitigation: nudge the rail content a few px every few minutes.
setInterval(() => {
  const dx = Math.round(Math.random() * 8 - 4);
  const dy = Math.round(Math.random() * 8 - 4);
  railInner.style.transform = `translate(${dx}px, ${dy}px)`;
}, 180_000);

function renderWeather(): void {
  const stage = modules.get("weather")?.stage;
  if (!stage?.current) return;
  const current = stage.current;
  const today = stage.daily?.[0];
  const alerts: any[] = (modules.get("weather_alerts")?.stage as any)?.alerts ?? [];
  const alertChip = alerts.length
    ? `<div class="weather-alert-chip">${WEATHER_ICONS.warning}<span>${alerts[0].event}${
        alerts.length > 1 ? ` +${alerts.length - 1}` : ""
      }</span></div>`
    : "";
  weatherEl.innerHTML = `
    <div class="weather-temp"><span class="weather-icon">${weatherIcon(
      current.code,
    )}</span>${Math.round(current.temp)}°</div>
    <div class="weather-text">${current.text ?? ""}</div>
    <div class="weather-meta">
      ${today ? `H ${Math.round(today.high)}° · L ${Math.round(today.low)}°` : ""}
    </div>
    <div class="weather-meta">${current.humidity != null ? `${current.humidity}% rh` : ""}
      ${current.wind != null ? ` · ${Math.round(current.wind)} mph` : ""}</div>
    ${alertChip}
    <div class="weather-loc">${stage.location ?? ""}</div>`;
}

// ---- Blank / wake ------------------------------------------------------------------

// Module updates that arrived while blanked and were not drawn.
let renderDeferred = false;

function blank(): void {
  blanked = true;
  // Pauses every CSS animation (tape, radar loop, sweeps) under the blanker —
  // they were compositing all night behind an opaque div.
  document.documentElement.dataset.idle = "";
  blanker.classList.remove("hidden");
  updateScoreChip();
  reportDisplayState();
}

function wake(): void {
  blanked = false;
  delete document.documentElement.dataset.idle;
  if (renderDeferred) {
    renderDeferred = false;
    rebuildTape();
    renderStage();
  }
  blanker.classList.add("hidden");
  updateScoreChip();
  reportDisplayState();
}

// ---- Remote control ----------------------------------------------------------------

function handleControl(action: string): void {
  switch (action) {
    case "next":
      rotation.next();
      break;
    case "prev":
      rotation.prev();
      break;
    case "pin":
      rotation.togglePin();
      break;
    case "blank":
      blank();
      break;
    case "wake":
      wake();
      break;
    case "reload":
      location.reload();
      break;
    case "starship_test":
      startStarshipPreview();
      break;
  }
}

// ---- Starship flight-day preview (admin "Test Starship card") ----------------
// Fabricated payload tour: the flight-day takeover card (10s), then the T−0
// countdown board (10s), then the real launches payload is restored. Runs on
// the display only — nothing is written to config or the backend.

let starshipPreviewTimers: number[] = [];
let starshipPreviewCleanup: (() => void) | null = null;

function starshipPreviewStage(minutesToNet: number): any {
  const live = minutesToNet <= 45;
  return {
    live,
    recent: [],
    launches: [{
      name: "Starship | Flight 13",
      provider: "SpaceX",
      mission: "Flight 13",
      mission_description:
        "Integrated flight test of Starship and Super Heavy — booster tower-catch attempt and payload bay door demonstration.",
      orbit: "Sub",
      orbit_name: "Suborbital",
      net: new Date(Date.now() + minutesToNet * 60000).toISOString(),
      status: "Go",
      status_text: "Go for Launch",
      pad: "Orbital Launch Mount A",
      location: "SpaceX Starbase, TX, USA",
      pad_count: 12,
      image: "",
      probability: 75,
      weather_concerns: null,
      programs: [],
      boosters: [{
        serial: "B14",
        flight_no: 2,
        reused: true,
        landing_attempt: true,
        landing_type: "RTLS",
        landing_location: "Tower catch",
      }],
      rocket: { full_name: "Starship", total: 12, successes: 8, streak: 3 },
      florida: false,
      starship: true,
      live,
    }],
  };
}

function startStarshipPreview(): void {
  starshipPreviewCleanup?.(); // restart cleanly on a second button press
  const real = modules.get("launches");
  const inserted = !rotation.order.includes("launches");
  if (inserted) rotation.order.push("launches");
  const wasPinned = rotation.pinned;
  const wasIndex = rotation.index;
  rotation.index = rotation.order.indexOf("launches");
  rotation.pinned = true; // hold the preview on screen; badge untouched

  const show = (stage: any) => {
    modules.set("launches", { module: "launches", stage, tape: [] } as any);
    renderStage();
  };
  starshipPreviewCleanup = () => {
    for (const t of starshipPreviewTimers) clearTimeout(t);
    starshipPreviewTimers = [];
    starshipPreviewCleanup = null;
    if (real) modules.set("launches", real);
    else modules.delete("launches");
    if (inserted) rotation.order = rotation.order.filter((id) => id !== "launches");
    rotation.pinned = wasPinned;
    rotation.index = Math.min(wasIndex, Math.max(0, rotation.order.length - 1));
    renderStage();
  };

  show(starshipPreviewStage(6 * 60)); // flight-day card, T−6h
  starshipPreviewTimers.push(
    window.setTimeout(() => show(starshipPreviewStage(9)), 10_000), // T−9m board
    window.setTimeout(() => starshipPreviewCleanup?.(), 20_000),
  );
}

// Debug/test hook: run the same preview from a DevTools/CDP session.
(window as any).__starshiptest = () => startStarshipPreview();

// ---- Gestures ----------------------------------------------------------------------

const appEl = document.getElementById("app")!;

// Each takeover dismisses itself on pointerdown, and its host div is a child of
// #app — so its listener runs (bubble) before the gesture recognizer resolves,
// and by then takeoverOpen() already reads false. Sample it in the capture
// phase instead, so the touch that dismissed a takeover does nothing else.
let pointerStartedOnTakeover = false;
appEl.addEventListener(
  "pointerdown",
  () => (pointerStartedOnTakeover = takeoverOpen()),
  true,
);

attachGestures(appEl, {
  onSwipe(direction) {
    if (pointerStartedOnTakeover) return;
    if (blanked) {
      wake();
      return;
    }
    if (overlay.isOpen()) {
      if (direction === "down") overlay.close();
      return;
    }
    switch (direction) {
      case "left":
        releaseAutoPin();
        rotation.next();
        rotation.schedule();
        break;
      case "right":
        releaseAutoPin();
        rotation.prev();
        rotation.schedule();
        break;
      case "up":
        overlay.open();
        break;
      case "down":
        blank();
        break;
    }
  },
  onTap(target) {
    if (pointerStartedOnTakeover) return;
    if (blanked) {
      wake();
      return;
    }
    if (overlay.isOpen()) return; // overlay handles its own clicks
    handleStageTap(target);
  },
  onLongPress() {
    if (pointerStartedOnTakeover) return;
    if (blanked || overlay.isOpen()) return;
    rotation.togglePin();
  },
});

connect();
