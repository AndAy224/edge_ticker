// Shared admin state: config draft (signals), health polling, live preview WS.

import { computed, signal } from "@preact/signals";

export const config = signal<any>(null);
const savedJson = signal("");
export const dirty = computed(
  () => config.value !== null && JSON.stringify(config.value) !== savedJson.value,
);
export const saveStatus = signal("");

export const health = signal<any>(null);
export const entities = signal<any[]>([]);
export const haStatus = signal("unknown");

// Live preview, fed by /ws/admin
export const displayState = signal<any>({});
export const livePayloads = signal<Record<string, any>>({});

export function patch(mutator: (cfg: any) => void): void {
  const next = structuredClone(config.value);
  mutator(next);
  config.value = next;
}

export async function loadConfig(): Promise<void> {
  const c = await (await fetch("/api/config")).json();
  config.value = c;
  savedJson.value = JSON.stringify(c);
  saveStatus.value = "";
}

export async function saveConfig(): Promise<void> {
  saveStatus.value = "saving…";
  const response = await fetch("/api/config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config.value),
  });
  if (response.ok) {
    savedJson.value = JSON.stringify(config.value);
    saveStatus.value = "applied ✓";
    setTimeout(() => (saveStatus.value = ""), 3000);
  } else {
    saveStatus.value = `save failed (${response.status})`;
  }
}

export function discardConfig(): void {
  config.value = JSON.parse(savedJson.value);
}

export async function refreshHealth(): Promise<void> {
  try {
    health.value = await (await fetch("/api/health")).json();
  } catch {
    health.value = null;
  }
}

export async function loadEntities(): Promise<void> {
  const data = await (await fetch("/api/ha/entities")).json();
  entities.value = data.entities ?? [];
  haStatus.value = data.status ?? "unknown";
}

export async function control(action: string): Promise<void> {
  await fetch("/api/control", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action }),
  });
}

export function connectWs(): void {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/admin`);
  ws.onclose = () => setTimeout(connectWs, 3000);
  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    switch (msg.type) {
      case "snapshot":
        livePayloads.value = msg.modules ?? {};
        displayState.value = msg.display_state ?? {};
        haStatus.value = msg.ha?.status ?? haStatus.value;
        break;
      case "module":
        livePayloads.value = {
          ...livePayloads.value,
          [msg.payload.module]: msg.payload,
        };
        break;
      case "display_state":
        displayState.value = msg.state ?? {};
        break;
      case "ha_status":
        haStatus.value = msg.status;
        break;
      case "config":
        // Another client saved config; adopt it unless we have local edits.
        if (!dirty.value) {
          config.value = msg.config;
          savedJson.value = JSON.stringify(msg.config);
        }
        break;
    }
  };
}
