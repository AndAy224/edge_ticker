// Shared admin state: config draft (signals), health polling, live preview WS.

import { computed, signal } from "@preact/signals";
import { isOutdated, reloadOnto } from "../../shared/build";

// The draft is edited locally and saved whole. Two facts keep that honest:
// `base` is the server document the draft started from (what Discard returns
// to, and what "dirty" compares against), and `baseVersion` is that document's
// version, sent as If-Match so a save made from a stale copy is refused (409)
// instead of silently reverting what another tab or a restore saved meanwhile.
// When a newer document arrives — another client's save over the WS, a
// reconnect snapshot, a 409 — local edits are rebased onto it rather than
// either being thrown away or ignoring it.
export const config = signal<any>(null);
const base = signal<any>(null);
let baseVersion: string | null = null;
const savedJson = computed(() => JSON.stringify(base.value));
export const dirty = computed(
  () => config.value !== null && JSON.stringify(config.value) !== savedJson.value,
);
export const saveStatus = signal("");
export const saving = signal(false);
export const loadError = signal("");

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

function isObject(v: unknown): v is Record<string, any> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function same(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (Array.isArray(a) && Array.isArray(b)) {
    return a.length === b.length && a.every((v, i) => same(v, b[i]));
  }
  if (isObject(a) && isObject(b)) {
    const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
    for (const k of keys) if (!same(a[k], b[k])) return false;
    return true;
  }
  return false;
}

/** `remote` with the edits `local` made to `older` replayed on top. Objects
 *  merge key by key; anything else (scalars, lists — as config history diffs
 *  them) is one value: the local one if it was edited, else the remote one. */
export function rebase(older: any, local: any, remote: any): any {
  if (isObject(older) && isObject(local) && isObject(remote)) {
    const out: Record<string, any> = { ...remote };
    for (const key of new Set([...Object.keys(older), ...Object.keys(local)])) {
      if (!(key in local)) delete out[key]; // removed locally
      else if (!(key in older)) out[key] = local[key]; // added locally
      else {
        const merged = rebase(older[key], local[key], remote[key]);
        if (merged === undefined) delete out[key];
        else out[key] = merged;
      }
    }
    return out;
  }
  return same(local, older) ? remote : local;
}

/** Take a server document as the new base, keeping any local edits on top. */
function adopt(remote: any, version: string | null, announce: boolean): void {
  if (remote == null) return;
  if (version !== null && version === baseVersion && config.value !== null) return;
  const hadEdits = dirty.value;
  config.value = hadEdits ? rebase(base.value, config.value, remote) : structuredClone(remote);
  base.value = remote;
  baseVersion = version;
  if (hadEdits && announce) {
    saveStatus.value = "config changed elsewhere — your unsaved edits are kept on top of it";
  }
}

function etagOf(response: Response): string | null {
  return response.headers.get("ETag")?.replace(/^W\//, "").replace(/"/g, "") || null;
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** (Re)load the server's config, discarding local edits. Retries until the
 *  backend answers — the admin is often opened right after a deploy restart. */
export async function loadConfig(): Promise<void> {
  for (let delay = 1000; ; delay = Math.min(delay * 2, 15_000)) {
    try {
      const response = await fetch("/api/config", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const c = await response.json();
      base.value = c;
      baseVersion = etagOf(response);
      config.value = structuredClone(c);
      saveStatus.value = "";
      loadError.value = "";
      return;
    } catch (err) {
      loadError.value = `can't load the config (${err instanceof Error ? err.message : err}) — retrying`;
      await sleep(delay);
    }
  }
}

export async function saveConfig(): Promise<void> {
  if (saving.value) return;
  saving.value = true;
  saveStatus.value = "saving…";
  // What is sent is what the server will hold: edits made while the request is
  // in flight stay dirty (they used to be marked saved without being sent).
  const sent = structuredClone(config.value);
  const sentFrom = base.value;
  try {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (baseVersion) headers["If-Match"] = `"${baseVersion}"`;
    const response = await fetch("/api/config", {
      method: "PUT",
      headers,
      body: JSON.stringify(sent),
    });
    const reply = await response.json().catch(() => null);
    if (response.ok) {
      // The stored document (defaults merged in) becomes the base; anything
      // edited since the click is replayed on top of it.
      const stored = reply?.config ?? sent;
      config.value = rebase(sent, config.value, stored);
      base.value = stored;
      baseVersion = reply?.version ?? etagOf(response);
      saveStatus.value = "applied ✓";
      setTimeout(() => {
        if (saveStatus.value === "applied ✓") saveStatus.value = "";
      }, 3000);
    } else if (response.status === 409 && reply?.config) {
      // Someone else saved first. Keep our edits on their document and let the
      // user look before saving again.
      config.value = rebase(sentFrom, config.value, reply.config);
      base.value = reply.config;
      baseVersion = reply.version ?? etagOf(response);
      saveStatus.value =
        "not saved: the config changed elsewhere — your edits are kept on top of it; review and save again";
    } else {
      // Validation failures name the offending keys; nothing was saved.
      saveStatus.value = `not saved: ${reply?.error ?? `HTTP ${response.status}`}`;
    }
  } catch {
    saveStatus.value = "not saved: the backend didn't answer — your edits are kept";
  } finally {
    saving.value = false;
  }
}

export function discardConfig(): void {
  config.value = structuredClone(base.value);
  saveStatus.value = "";
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

/** Fire a control action. Returns the parsed reply so a caller can surface an
 *  error — the test actions can legitimately refuse (e.g. no cameras
 *  configured), and a silent no-op reads as a broken button. */
export async function control(action: string): Promise<any> {
  try {
    const res = await fetch("/api/control", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }),
    });
    return await res.json();
  } catch (err) {
    return { error: String(err) };
  }
}

export function connectWs(): void {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/admin`);
  ws.onclose = () => setTimeout(connectWs, 3000);
  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    switch (msg.type) {
      case "snapshot":
        if (isOutdated(msg.build?.admin)) {
          // A deploy happened. Reload only when that can't lose an edit.
          if (!dirty.value && reloadOnto(msg.build.admin)) return;
          saveStatus.value = "a new admin version is deployed — reload after saving";
        }
        livePayloads.value = msg.modules ?? {};
        displayState.value = msg.display_state ?? {};
        haStatus.value = msg.ha?.status ?? haStatus.value;
        // A reconnect (every deploy) may have missed config broadcasts: the
        // snapshot's copy is the current one. Before the first load finishes
        // there is no draft to protect, and loadConfig sets the base itself.
        if (config.value !== null) adopt(msg.config, msg.config_version ?? null, true);
        break;
      case "module":
        livePayloads.value = {
          ...livePayloads.value,
          [msg.payload.module]: msg.payload,
        };
        break;
      case "module_removed": {
        const { [msg.module]: _gone, ...rest } = livePayloads.value;
        livePayloads.value = rest;
        break;
      }
      case "display_state":
        displayState.value = msg.state ?? {};
        break;
      case "ha_status":
        haStatus.value = msg.status;
        break;
      case "config":
        // A save landed (ours, another tab's, or a restore). Our own arrives
        // while the PUT is in flight and is then adopted from its reply.
        if (config.value !== null && !saving.value) {
          adopt(msg.config, msg.version ?? null, true);
        }
        break;
    }
  };
}
