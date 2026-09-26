import { NET_ICONS } from "../icons";
import { register } from "./registry";

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!,
  );
}

/** Same units as the collector's tape text (backend fmt_rate). */
function rate(bps: number | null | undefined): string {
  if (bps == null) return "—";
  if (bps >= 1e9) return `${(bps / 1e9).toFixed(2)} Gb/s`;
  if (bps >= 1e6) return `${(bps / 1e6).toFixed(bps < 1e8 ? 1 : 0)} Mb/s`;
  return `${Math.round(bps / 1e3)} kb/s`;
}

function compact(n: number): string {
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e4) return `${Math.round(n / 1e3)}k`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
  return String(n);
}

/** "for 2h 14m" from the collector's ISO switch time. Refreshed on every
 *  poll's re-render (15 s), which is plenty at minute resolution. */
function elapsed(iso: string | null | undefined): string {
  if (!iso) return "";
  const minutes = Math.max(0, Math.floor((Date.now() - Date.parse(iso)) / 60000));
  if (Number.isNaN(minutes)) return "";
  if (minutes < 60) return `for ${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `for ${hours}h ${minutes % 60}m`;
  return `for ${Math.floor(hours / 24)}d ${hours % 24}h`;
}

let gradSeq = 0;

/** Area + line on a 100×28 box, zero-based with a floor on the top of the
 *  scale: min-max scaling turns 0.2 ms of jitter into a full-height saw.
 *  `second` (upload) shares the scale as a thin line so the two directions
 *  read against each other. */
function spark(values: number[], cls: string, second?: number[], floor = 1): string {
  if (!values || values.length < 2) return `<div class="opn-spark-empty"></div>`;
  const max = Math.max(floor, ...values, ...(second ?? []));
  const pts = (series: number[]) =>
    series
      .map((v, i) => `${((i / (series.length - 1)) * 100).toFixed(1)},${(26 - (v / max) * 24).toFixed(1)}`)
      .join(" ");
  const id = `opn-grad-${++gradSeq}`;
  const main = pts(values);
  return `<svg class="opn-spark ${cls}" viewBox="0 0 100 28" preserveAspectRatio="none">
    <defs><linearGradient id="${id}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" class="opn-fill-top"/><stop offset="100%" class="opn-fill-bottom"/>
    </linearGradient></defs>
    <polygon fill="url(#${id})" stroke="none" points="0,28 ${main} 100,28"/>
    <polyline class="opn-line" points="${main}"/>
    ${second && second.length >= 2 ? `<polyline class="opn-line2" points="${pts(second)}"/>` : ""}
  </svg>`;
}

const PILL: Record<string, string> = {
  up: "STANDBY",
  warn: "DEGRADED",
  down: "DOWN",
  nolink: "NO LINK",
};

function pill(w: any): string {
  const text = w.active ? (w.health === "warn" ? "ACTIVE · DEGRADED" : "ACTIVE") : PILL[w.health] ?? "—";
  const tone = w.health === "up" ? (w.active ? "good" : "idle") : w.health === "warn" ? "warn" : "bad";
  return `<span class="opn-pill ${tone}">${text}</span>`;
}

function num(value: number | null | undefined, digits = 0): string {
  return value == null ? "—" : value.toFixed(digits);
}

function lineRate(bps: number | null | undefined): string {
  if (!bps) return "";
  return bps >= 1e9 ? `${bps / 1e9} Gb/s link` : `${bps / 1e6} Mb/s link`;
}

function wanCard(w: any, minutes: number): string {
  const history = w.history ?? {};
  const alive = w.health === "up" || w.health === "warn";
  const meta = [
    w.ip,
    lineRate(w.line_bps),
    w.monitor ? `probing ${w.monitor}` : "",
  ].filter(Boolean);
  const body = alive
    ? `<div class="opn-stats">
        <span class="opn-lat"><b>${num(w.latency_ms)}</b><i>ms</i></span>
        <span class="opn-stat"><b>${num(w.loss_pct)}%</b><i>loss</i></span>
        <span class="opn-stat"><b>${num(w.jitter_ms, 1)}</b><i>ms jitter</i></span>
      </div>
      ${spark(history.latency ?? [], `latency ${w.health}`, undefined, 20)}
      <div class="opn-rates">
        <span class="opn-down">▼ ${rate(w.down_bps)}</span>
        <span class="opn-upl">▲ ${rate(w.up_bps)}</span>
        <span class="opn-window">${minutes} min</span>
      </div>
      ${spark(history.down ?? [], "traffic", history.up ?? [], 1000)}`
    : `<div class="opn-dead">
        <b>${w.health === "nolink" ? "No link" : "Gateway down"}</b>
        <span>${escapeHtml(
          w.health === "nolink"
            ? `port ${w.device ?? "?"} · ${w.link}`
            : `${w.gateway ?? "gateway"} ${String(w.status_text ?? "offline").toLowerCase()}`,
        )}</span>
      </div>`;
  return `<div class="opn-card opn-wan ${w.active ? "active" : ""} ${escapeHtml(w.health)}">
    <div class="opn-card-head">
      <span class="opn-icon">${NET_ICONS[w.kind] ?? NET_ICONS.wired}</span>
      <span class="opn-name">${escapeHtml(w.name)}</span>
      ${pill(w)}
    </div>
    ${body}
    ${meta.length ? `<div class="opn-meta">${meta.map(escapeHtml).join(" · ")}</div>` : ""}
  </div>`;
}

function hero(data: any): string {
  const wans: any[] = data.wans ?? [];
  const active = data.active;
  const current = wans.find((w) => w.active);
  const primary = wans.find((w) => w.primary);
  let tone = "good";
  let title = `ONLINE <span class="opn-via">· ${escapeHtml(active?.name ?? "")}</span>`;
  let sub: string[] = [primary && active?.id === primary.id ? "primary WAN" : ""];
  if (data.offline) {
    tone = "bad";
    title = "OFFLINE";
    sub = ["no WAN gateway online"];
  } else if (data.failover) {
    tone = "alert";
    title = `FAILOVER <span class="opn-via">· ${escapeHtml(active?.name ?? "")}</span>`;
    const why =
      primary && primary.health === "nolink"
        ? primary.link
        : primary && primary.health === "down"
          ? "gateway down"
          : "bypassed";
    sub = [primary ? `${primary.name} ${why}` : ""];
  }
  if (active?.ip) sub.push(active.ip);
  sub.push(elapsed(active?.since));
  return `<div class="opn-hero ${tone}">
    <span class="opn-hero-icon">${NET_ICONS[active?.kind] ?? NET_ICONS.wired}</span>
    <div class="opn-hero-text">
      <div class="opn-hero-title">${title}</div>
      <div class="opn-hero-sub">${sub.filter(Boolean).map(escapeHtml).join(" · ")}</div>
    </div>
    ${
      current
        ? `<div class="opn-hero-rates">
            <span class="opn-down">▼ ${rate(current.down_bps)}</span>
            <span class="opn-upl">▲ ${rate(current.up_bps)}</span>
          </div>`
        : ""
    }
  </div>`;
}

/** Rows scaled to the busiest network — absolute scale would flatline every
 *  bar on a 10 Gb/s port (the pve guest-table trap). */
function networks(vlans: any[]): string {
  const total = (v: any) => (v.down_bps ?? 0) + (v.up_bps ?? 0);
  const scale = Math.max(8000, ...vlans.map(total));
  return `<div class="opn-card opn-nets">
    <div class="opn-side-title">Networks</div>
    <div class="opn-net-rows">${vlans
      .map(
        (v) => `<div class="opn-net">
          <span class="opn-net-name">${escapeHtml(v.name)}</span>
          <span class="opn-mini"><span style="width:${Math.min(100, (total(v) / scale) * 100).toFixed(1)}%"></span></span>
          <span class="opn-net-rate">▼ ${rate(v.down_bps)}</span>
          <span class="opn-net-rate">▲ ${rate(v.up_bps)}</span>
        </div>`,
      )
      .join("")}</div>
  </div>`;
}

function firewall(s: any): string {
  if (!s) return "";
  const cells: [string, string][] = [];
  if (s.states != null) cells.push(["states", compact(s.states)]);
  if (s.load != null) cells.push(["load", s.load.toFixed(2)]);
  if (s.mem_pct != null) cells.push(["memory", `${Math.round(s.mem_pct)}%`]);
  if (s.uptime) cells.push(["up", s.uptime]);
  return `<div class="opn-card opn-fw">
    <div class="opn-side-title">Firewall${s.version ? ` <span class="opn-dim">OPNsense ${escapeHtml(s.version)}</span>` : ""}</div>
    <div class="opn-fw-grid">${cells
      .map(([k, v]) => `<span class="opn-fw-cell"><b>${escapeHtml(v)}</b><i>${k}</i></span>`)
      .join("")}</div>
  </div>`;
}

register({
  id: "opnsense",
  renderStage(el, data) {
    const wans: any[] = (data?.wans ?? []).slice(0, 3);
    const minutes = data?.history_minutes ?? 60;
    el.innerHTML = `<div class="opn-layout ${data?.failover ? "failover" : ""} ${data?.offline ? "offline" : ""}">
      <div class="opn-main">
        ${hero(data ?? {})}
        <div class="opn-wans">${wans.map((w) => wanCard(w, minutes)).join("")}</div>
      </div>
      <div class="opn-side">
        ${networks((data?.vlans ?? []).slice(0, 8))}
        ${firewall(data?.system)}
      </div>
    </div>`;
  },
});
