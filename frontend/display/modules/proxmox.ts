import { register } from "./registry";

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!,
  );
}

function bar(pct: number, label: string): string {
  const hot = pct >= 90;
  return `<div class="pve-bar-row">
    <span class="pve-bar-label">${label}</span>
    <span class="pve-bar"><span class="pve-bar-fill ${hot ? "hot" : ""}" style="width:${Math.min(pct, 100)}%"></span></span>
    <span class="pve-bar-pct">${pct.toFixed(0)}%</span>
  </div>`;
}

function uptime(seconds: number): string {
  const days = Math.floor(seconds / 86400);
  if (days > 0) return `${days}d`;
  return `${Math.floor(seconds / 3600)}h`;
}

/** GiB in, human out — a 26 TB vault shouldn't read as 25970 GiB. */
function size(gb: number): string {
  if (gb == null) return "";
  return gb >= 1024 ? `${(gb / 1024).toFixed(1)} TiB` : `${Math.round(gb)} GiB`;
}

function storageRow(s: any): string {
  const hot = s.pct >= 90;
  return `<div class="pve-store">
    <div class="pve-store-top">
      <span class="pve-store-name">${escapeHtml(s.name)}</span>
      <span class="pve-bar-pct ${hot ? "hot" : ""}">${s.pct.toFixed(0)}%</span>
    </div>
    <span class="pve-bar"><span class="pve-bar-fill ${hot ? "hot" : ""}" style="width:${Math.min(s.pct, 100)}%"></span></span>
    ${
      s.free_gb != null
        ? `<div class="pve-store-free">${size(s.free_gb)} free of ${size(s.total_gb)}</div>`
        : ""
    }
  </div>`;
}

/** One row per running guest, bar scaled to the busiest one on the board.
 *  Absolute width would leave every bar at 2% on an idle host — the same trap
 *  the markets watchlist and the rain band hit. The floor keeps a quiet node
 *  from drawing a full bar for a 3% guest. */
function guestRow(g: any, scale: number): string {
  return `<div class="pve-guest">
    <span class="pve-vmid">${escapeHtml(g.vmid ?? "")}</span>
    <span class="pve-guest-name"><span class="pve-dot"></span>${escapeHtml(g.name)}</span>
    <span class="pve-guest-cpu">
      <span class="pve-mini"><span style="width:${Math.min(100, (g.cpu / scale) * 100).toFixed(1)}%"></span></span>
      <span class="pve-guest-pct">${g.cpu.toFixed(0)}%</span>
    </span>
    <span class="pve-guest-mem">${g.mem_gb != null ? `${g.mem_gb.toFixed(1)} GiB` : ""}</span>
  </div>`;
}

/** One PSU feed. Bars are scaled to the busiest feed in the group rather than
 *  to any absolute wattage: on a dual-PSU host the interesting signal is the
 *  *balance* between the two, and an absolute scale would pin both near zero. */
function feedRow(f: any, scale: number): string {
  const w = f.watts ?? 0;
  return `<div class="pve-feed${f.live ? "" : " dead"}">
    <span class="pve-feed-label">${escapeHtml(f.label)}</span>
    <span class="pve-mini"><span style="width:${Math.min(100, (w / scale) * 100).toFixed(1)}%"></span></span>
    <span class="pve-feed-w">${f.relay_on ? `${w.toFixed(0)} W` : "OFF"}</span>
  </div>`;
}

/** PDU-metered draw for the node's PSU feeds. Absent whenever the PDU isn't
 *  configured, so every caller has to tolerate an empty string. */
function powerBlock(p: any): string {
  if (!p) return "";
  const feeds: any[] = p.feeds ?? [];
  const scale = Math.max(1, ...feeds.map((f) => f.watts ?? 0));
  const state =
    p.state === "degraded"
      ? "no redundancy"
      : p.state === "off"
        ? "no draw"
        : feeds.length > 1
          ? "redundant"
          : "live";
  return `<div class="pve-power ${escapeHtml(p.state)}${p.stale ? " stale" : ""}">
    <div class="pve-power-head">
      <span class="pve-power-total">${(p.total_w ?? 0).toFixed(0)}<span class="pve-power-unit">W</span></span>
      <span class="pve-power-state">${state}</span>
    </div>
    <div class="pve-feeds">${feeds.map((f) => feedRow(f, scale)).join("")}</div>
  </div>`;
}

/** The power block belongs to exactly one node. An unset `node` means the
 *  collector saw a single host and didn't need telling which. */
function powerFor(power: any, node: any): any {
  if (!power) return null;
  return !power.node || power.node === node.name ? power : null;
}

function nodeCard(n: any, power: any): string {
  return `<div class="pve-card ${n.online ? "" : "offline"}">
    <div class="pve-card-head">
      <span class="pve-name">${escapeHtml(n.name)}</span>
      <span class="pve-up">${n.online ? `up ${uptime(n.uptime)}` : "OFFLINE"}</span>
    </div>
    ${bar(n.cpu, "CPU")}
    ${bar(n.mem_pct, "MEM")}
    <div class="pve-meta">${n.mem_used_gb} / ${n.mem_total_gb} GiB</div>
    ${powerBlock(power)}
  </div>`;
}

register({
  id: "proxmox",
  renderStage(el, data) {
    // 6 node cards fit the wide-pane grid (2 rows); narrower panes are
    // capped further in CSS via [data-panes] nth-child.
    const nodes: any[] = (data?.nodes ?? []).slice(0, 6);
    const guests = data?.guests ?? { running: 0, total: 0 };
    const busiest: any[] = guests.busiest ?? [];
    const storage: any[] = data?.storage ?? [];
    const power = data?.power ?? null;

    // A single node leaves most of its card empty, so it grows a guest table.
    // With a cluster the node cards fill the row on their own.
    if (nodes.length === 1 && busiest.length) {
      const node = nodes[0];
      const scale = Math.max(10, ...busiest.map((g) => g.cpu ?? 0));
      el.innerHTML = `<div class="pve-layout pve-solo">
        <div class="pve-card pve-node ${node.online ? "" : "offline"}">
          <div class="pve-card-head">
            <span class="pve-name">${escapeHtml(node.name)}</span>
            <span class="pve-up">${
              node.online
                ? `up ${uptime(node.uptime)} · ${guests.running}/${guests.total} running`
                : "OFFLINE"
            }</span>
          </div>
          ${bar(node.cpu, "CPU")}
          ${bar(node.mem_pct, "MEM")}
          <div class="pve-meta">${node.mem_used_gb} / ${node.mem_total_gb} GiB</div>
          ${powerBlock(powerFor(power, node))}
          <div class="pve-guests">
            <div class="pve-guest pve-guests-head">
              <span>VMID</span><span>GUEST</span><span>CPU</span><span>MEM</span>
            </div>
            ${busiest.map((g) => guestRow(g, scale)).join("")}
          </div>
        </div>
        <div class="pve-side">
          <div class="pve-card pve-storage">
            ${storage.map(storageRow).join("")}
          </div>
        </div>
      </div>`;
      return;
    }

    el.innerHTML = `<div class="pve-layout">
      <div class="pve-nodes">${nodes.map((n) => nodeCard(n, powerFor(power, n))).join("")}</div>
      <div class="pve-side">
        <div class="pve-card">
          <div class="pve-name">Guests</div>
          <div class="pve-big">${guests.running}<span class="pve-dim">/${guests.total}</span></div>
          <div class="pve-meta">running</div>
        </div>
        <div class="pve-card pve-storage">
          ${storage.slice(0, 4).map((s) => bar(s.pct, escapeHtml(s.name))).join("")}
        </div>
      </div>
    </div>`;
  },
});
