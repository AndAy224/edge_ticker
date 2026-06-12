// Display orchestrator: WS client, rotation engine, zone rendering, gestures.

import "./styles.css";
import { attachGestures } from "./gestures";
import { getRenderer, hasRenderer } from "./modules/registry";
import "./modules/markets";
import "./modules/news";
import "./modules/sports";
import { HAOverlay } from "./overlay-ha";
import { Tape } from "./tape";
import type { Config, ModulePayload } from "./types";

const stageEl = document.getElementById("stage-content")!;
const dotsEl = document.getElementById("page-dots")!;
const pinBadge = document.getElementById("pin-badge")!;
const railInner = document.getElementById("rail-inner")!;
const clockEl = document.getElementById("clock")!;
const dateEl = document.getElementById("date")!;
const weatherEl = document.getElementById("weather")!;
const blanker = document.getElementById("blanker")!;
const connDot = document.getElementById("conn-dot")!;

const modules = new Map<string, ModulePayload>();
let config: Config = {};
let blanked = false;
let detailTimer = 0;
let detailOpen = false;

const tape = new Tape(document.getElementById("tape-track")!);
const overlay = new HAOverlay(
  document.getElementById("overlay")!,
  (domain, service, entityId, data) =>
    sendWs({ type: "ha_action", domain, service, entity_id: entityId, data }),
);

// ---- WebSocket -------------------------------------------------------------

let ws: WebSocket | null = null;
let reconnectDelay = 1000;

function sendWs(message: Record<string, unknown>): void {
  if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify(message));
}

function connect(): void {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws/display`);
  ws.onopen = () => {
    reconnectDelay = 1000;
    connDot.classList.add("hidden");
  };
  ws.onclose = () => {
    connDot.classList.remove("hidden");
    setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 15_000);
  };
  ws.onmessage = (event) => handleMessage(JSON.parse(event.data));
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
      applyConfig();
      renderWeather();
      break;
    case "module": {
      const payload: ModulePayload = msg.payload;
      modules.set(payload.module, payload);
      if (payload.module === "weather") renderWeather();
      rebuildTape();
      if (payload.module === rotation.current() && !detailOpen) renderStage();
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
    case "ha_state":
      overlay.updateState(msg.entity_id, { state: msg.state, attributes: msg.attributes });
      break;
    case "ha_states":
      overlay.setStates(msg.states ?? {}, msg.status);
      break;
    case "ha_status":
      overlay.setStatus(msg.status);
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
      if (!this.pinned && !overlay.isOpen() && !blanked && !detailOpen) this.next();
    }, seconds * 1000);
  },
  next(): void {
    if (!this.order.length) return;
    this.index = (this.index + 1) % this.order.length;
    closeDetail();
    renderStage();
  },
  prev(): void {
    if (!this.order.length) return;
    this.index = (this.index - 1 + this.order.length) % this.order.length;
    closeDetail();
    renderStage();
  },
  togglePin(): void {
    this.pinned = !this.pinned;
    pinBadge.classList.toggle("hidden", !this.pinned);
  },
};

function applyConfig(): void {
  rotation.order = (config.rotation?.order ?? []).filter(
    (id) => hasRenderer(id) && config.modules?.[id]?.enabled !== false,
  );
  if (rotation.index >= rotation.order.length) rotation.index = 0;
  rotation.schedule();
  rebuildTape();
  renderStage();
}

// ---- Stage -------------------------------------------------------------------

function renderStage(): void {
  const id = rotation.current();
  renderDots();
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
  crossfade(layer);
}

function crossfade(layer: HTMLElement): void {
  const previous = Array.from(stageEl.children) as HTMLElement[];
  layer.classList.add("enter");
  stageEl.appendChild(layer);
  requestAnimationFrame(() => layer.classList.remove("enter"));
  for (const el of previous) {
    el.classList.add("exit");
    setTimeout(() => el.remove(), 320);
  }
}

function renderDots(): void {
  dotsEl.innerHTML = rotation.order
    .map((_, i) => `<span class="dot ${i === rotation.index ? "active" : ""}"></span>`)
    .join("");
}

// ---- Tap-to-expand detail ------------------------------------------------------

function handleStageTap(target: EventTarget | null): void {
  if (detailOpen) {
    closeDetail();
    renderStage();
    return;
  }
  const id = rotation.current();
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
  crossfade(layer);
  detailOpen = true;
  clearTimeout(detailTimer);
  detailTimer = window.setTimeout(() => {
    closeDetail();
    renderStage();
  }, 20_000);
}

function closeDetail(): void {
  detailOpen = false;
  clearTimeout(detailTimer);
}

// ---- Tape ----------------------------------------------------------------------

function rebuildTape(): void {
  const order = [...rotation.order, "weather"];
  const items = order.flatMap((id) =>
    [...(modules.get(id)?.tape ?? [])].sort((a, b) => b.priority - a.priority),
  );
  tape.setItems(items);
}

// ---- Status rail -----------------------------------------------------------------

function tickClock(): void {
  const now = new Date();
  clockEl.textContent = now.toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
  });
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
    <div class="weather-temp">${Math.round(current.temp)}°</div>
    <div class="weather-text">${current.text ?? ""}</div>
    <div class="weather-meta">
      ${today ? `H ${Math.round(today.high)}° · L ${Math.round(today.low)}°` : ""}
    </div>
    <div class="weather-meta">${current.humidity != null ? `${current.humidity}% rh` : ""}
      ${current.wind != null ? ` · ${Math.round(current.wind)} mph` : ""}</div>`;
}

// ---- Blank / wake ------------------------------------------------------------------

function blank(): void {
  blanked = true;
  blanker.classList.remove("hidden");
}

function wake(): void {
  blanked = false;
  blanker.classList.add("hidden");
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
