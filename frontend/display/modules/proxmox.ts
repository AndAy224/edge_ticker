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

register({
  id: "proxmox",
  renderStage(el, data) {
    const nodes: any[] = data?.nodes ?? [];
    const guests = data?.guests ?? { running: 0, total: 0 };
    const storage: any[] = data?.storage ?? [];
    el.innerHTML = `<div class="pve-layout">
      <div class="pve-nodes">${nodes
        .map(
          (n) => `<div class="pve-card ${n.online ? "" : "offline"}">
            <div class="pve-card-head">
              <span class="pve-name">${escapeHtml(n.name)}</span>
              <span class="pve-up">${n.online ? `up ${uptime(n.uptime)}` : "OFFLINE"}</span>
            </div>
            ${bar(n.cpu, "CPU")}
            ${bar(n.mem_pct, "MEM")}
            <div class="pve-meta">${n.mem_used_gb} / ${n.mem_total_gb} GiB</div>
          </div>`,
        )
        .join("")}</div>
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
