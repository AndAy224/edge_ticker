// Display orchestrator: WS client, rotation engine, zone rendering, gestures.

import "./styles.css";
import { attachGestures } from "./gestures";
import { getRenderer, hasRenderer } from "./modules/registry";
import "./modules/markets";
import "./modules/news";
import { setSportsLiveMode } from "./modules/sports";
import "./modules/adsb";
import "./modules/astro";
import "./modules/proxmox";
import "./modules/weather";
import { Celebration } from "./celebrate";
import { HAOverlay } from "./overlay-ha";
import { Tape } from "./tape";
import type { Config, ModulePayload } from "./types";
import { weatherIcon } from "./icons";
import { DEFAULT_LAYOUT, DEFAULT_THEME, LAYOUTS, THEMES } from "../shared/themes";

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

const modules = new Map<string, ModulePayload>();
const haStates = new Map<string, any>(); // alert entities (and mapped) by id
let config: Config = {};
let blanked = false;
const scoreChip = document.getElementById("score-chip")!;

// Multi-pane stage: layouts can show a window of 1–3 consecutive rotation
// modules side by side. Pane i shows rotation.order[(index + i) % length].
let paneEls: HTMLElement[] = [];
const paneDetailTimers = new Map<number, number>(); // pane index -> auto-close timer

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
    location.reload();
  }
}, 15_000);

function reportDisplayState(): void {
  sendWs({
    type: "display_state",
    state: {
      module: rotation.current() ?? null,
      pinned: rotation.pinned,
      blanked,
      overlay: overlay.isOpen(),
    },
  });
}

function handleMessage(msg: any): void {
  switch (msg.type) {
    case "snapshot":
      modules.clear();
      for (const [name, payload] of Object.entries(msg.modules ?? {})) {
        modules.set(name, payload as ModulePayload);
      }
      config = msg.config ?? {};
      overlay.setMapping(config.ha);
      overlay.setStates(msg.ha?.states ?? {}, msg.ha?.status);
      haStates.clear();
      for (const [id, s] of Object.entries(msg.ha?.states ?? {})) haStates.set(id, s);
      applyConfig();
      renderWeather();
      updateScoreChip();
      break;
    case "module": {
      const payload: ModulePayload = msg.payload;
      modules.set(payload.module, payload);
      if (payload.module === "weather") renderWeather();
      if (payload.module === "sports") {
        autoFeatureSports(payload);
        updateScoreChip();
      }
      rebuildTape();
      for (let i = 0; i < paneEls.length; i++) {
        if (paneModule(i) === payload.module && !paneDetailTimers.has(i)) {
          renderPane(i);
        }
      }
      break;
    }
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
      // Software dim fallback when DDC/CI isn't available.
      dimmer.style.opacity =
        msg.mode === "dim" ? String(1 - (msg.level ?? 10) / 100) : "0";
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
    const seconds = config.rotation?.interval_seconds ?? 25;
    this.timer = window.setInterval(() => {
      if (!this.pinned && !overlay.isOpen() && !blanked && !anyDetailOpen()) this.next();
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
    pinBadge.classList.toggle("hidden", !this.pinned);
    reportDisplayState();
  },
};

function applyConfig(): void {
  applyAppearance();
  setSportsLiveMode((config.modules?.sports as any)?.live_mode !== false);
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
  const header = paneEls[i].querySelector<HTMLElement>(".pane-header");
  if (header) header.textContent = paneLabel(id);
  const layer = document.createElement("div");
  layer.className = "stage-layer";
  if (!id) {
    layer.innerHTML = `<div class="empty">No modules enabled</div>`;
  } else {
    const payload = modules.get(id);
    const renderer = getRenderer(id);
    if (!payload || !renderer) {
      layer.innerHTML = `<div class="empty">Waiting for ${id} data…</div>`;
    } else {
      renderer.renderStage(layer, payload.stage);
      if (payload.stale) {
        const dot = document.createElement("span");
        dot.className = "stale-dot";
        dot.title = "data is stale";
        layer.appendChild(dot);
      }
    }
  }
  crossfade(paneEls[i], layer);
}

function crossfade(container: HTMLElement, layer: HTMLElement): void {
  // Only layers fade out — panes also hold a persistent .pane-header.
  const previous = Array.from(container.querySelectorAll(":scope > .stage-layer"));
  layer.classList.add("enter");
  container.appendChild(layer);
  requestAnimationFrame(() => layer.classList.remove("enter"));
  for (const el of previous) {
    el.classList.add("exit");
    setTimeout(() => el.remove(), 320);
  }
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
  if (paneDetailTimers.has(pane)) {
    closeDetail(pane);
    renderPane(pane);
    return;
  }
  const id = paneModule(pane);
  if (!id) return;
  const renderer = getRenderer(id);
  const payload = modules.get(id);
  const key = (target as HTMLElement)?.closest?.<HTMLElement>("[data-detail]")?.dataset
    .detail;
  if (!renderer?.renderDetail || !renderer.getDetailItem || !payload || key == null) return;
  const item = renderer.getDetailItem(payload.stage, key);
  if (!item) return;

  const layer = document.createElement("div");
  layer.className = "stage-layer";
  renderer.renderDetail(layer, item);
  crossfade(paneEls[pane], layer);
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
// and pin. Edge-triggered: a manual unpin isn't fought until the next game
// starts; when no followed game is live anymore, auto-unpin.
let liveFeatured = false;

function autoFeatureSports(payload: ModulePayload): void {
  const games: any[] = payload.stage?.games ?? [];
  const liveFollowed = games.some((g) => g.followed && g.state === "in");
  if (liveFollowed && !liveFeatured) {
    liveFeatured = true;
    if (
      config.modules?.sports?.auto_feature === true &&
      !blanked &&
      !overlay.isOpen() &&
      !anyDetailOpen() &&
      !rotation.pinned
    ) {
      const target = rotation.order.indexOf("sports");
      if (target >= 0) {
        rotation.index = target;
        rotation.pinned = true;
        pinBadge.classList.remove("hidden");
        renderStage();
        reportDisplayState();
      }
    }
  } else if (!liveFollowed && liveFeatured) {
    liveFeatured = false;
    if (rotation.pinned && config.modules?.sports?.auto_feature === true) {
      rotation.pinned = false;
      pinBadge.classList.add("hidden");
      reportDisplayState();
    }
  }
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
  // Set-dedupe: weather always contributes, but only once if it's in rotation.
  const order = [...new Set([...rotation.order, "weather"])];
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

// Debug/test hook: replace the sports payload entirely and re-render.
(window as any).__sportsfake = (games: any[]) => {
  modules.set("sports", { module: "sports", stage: { games }, tape: [] } as any);
  renderStage();
  updateScoreChip();
};

// Debug/test hook: inject a fabricated live game and refresh the chip.
(window as any).__scorechip = (game: any) => {
  const payload: any = modules.get("sports") ?? { module: "sports", stage: {}, tape: [] };
  payload.stage = { games: [game] };
  modules.set("sports", payload);
  updateScoreChip();
};

// ---- Status rail -----------------------------------------------------------------

function tickClock(): void {
  const now = new Date();
  const time = now.toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
  });
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
    <div class="weather-loc">${stage.location ?? ""}</div>`;
}

// ---- Blank / wake ------------------------------------------------------------------

function blank(): void {
  blanked = true;
  blanker.classList.remove("hidden");
  updateScoreChip();
  reportDisplayState();
}

function wake(): void {
  blanked = false;
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
  }
}

// ---- Gestures ----------------------------------------------------------------------

attachGestures(document.getElementById("app")!, {
  onSwipe(direction) {
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
        rotation.next();
        rotation.schedule();
        break;
      case "right":
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
    if (blanked) {
      wake();
      return;
    }
    if (overlay.isOpen()) return; // overlay handles its own clicks
    handleStageTap(target);
  },
  onLongPress() {
    if (blanked || overlay.isOpen()) return;
    rotation.togglePin();
  },
});

connect();
