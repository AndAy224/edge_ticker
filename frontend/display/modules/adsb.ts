import { register } from "./registry";

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!,
  );
}

register({
  id: "adsb",
  renderStage(el, data) {
    const aircraft: any[] = data?.aircraft ?? [];
    if (!aircraft.length) {
      el.innerHTML = `<div class="empty">No aircraft within ${data?.radius_km ?? "?"} km</div>`;
      return;
    }
    el.innerHTML = `<div class="adsb-list">
      <div class="adsb-header">
        <span>✈ overhead now</span>
        <span class="adsb-count">${data.count_in_radius} within ${data.radius_km} km · ${data.count_total} tracked</span>
      </div>
      ${aircraft
        .map(
          (a) => `<div class="adsb-row">
            <span class="adsb-flight">${escapeHtml(a.flight)}</span>
            <span class="adsb-alt">${a.alt_ft != null ? `${Number(a.alt_ft).toLocaleString()} ft` : "—"}</span>
            <span class="adsb-speed">${a.speed_kt != null ? `${Math.round(a.speed_kt)} kt` : "—"}</span>
            <span class="adsb-dist">${a.distance_km} km ${escapeHtml(a.direction)}</span>
            <span class="adsb-track" style="transform: rotate(${(a.track ?? 0) - 90}deg)">➤</span>
          </div>`,
        )
        .join("")}
    </div>`;
  },
});
