import { register } from "./registry";
import { AIRCRAFT_ICON, AIRCRAFT_PATH } from "../icons";

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!,
  );
}

// --- radar scope geometry (200×200 viewBox, centred on the receiver) ---
const VIEW = 200;
const CX = 100;
const CY = 100;
const MAX_R = 92; // plotted radius; leaves a margin for compass letters
const PLANE_SCALE = 0.52; // 24-unit icon → ~12.5 units on the scope

// Climb / descent chevrons (no emoji fonts on the device → inline SVG).
const CLIMB =
  `<svg class="vr-arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" ` +
  `stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M6 15l6-6 6 6"/></svg>`;
const DESCEND =
  `<svg class="vr-arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" ` +
  `stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>`;

function altBand(a: any): string {
  if (a?.on_ground) return "alt-low";
  const alt = a?.alt_ft;
  if (alt == null) return "alt-unknown";
  if (alt < 5000) return "alt-low";
  if (alt < 20000) return "alt-mid";
  return "alt-high";
}

function fmtAlt(a: any): string {
  if (a?.on_ground) return "ground";
  return a?.alt_ft != null ? `${Number(a.alt_ft).toLocaleString()} ft` : "—";
}

function polar(distanceKm: number, radiusKm: number, bearingDeg: number) {
  const frac = radiusKm > 0 ? Math.min(distanceKm / radiusKm, 1) : 0;
  const r = frac * MAX_R;
  const rad = (bearingDeg * Math.PI) / 180;
  return { x: CX + r * Math.sin(rad), y: CY - r * Math.cos(rad) };
}

function scopeSvg(aircraft: any[], radiusKm: number): string {
  const rings = [0.25, 0.5, 0.75, 1]
    .map((f) => {
      const rr = (f * MAX_R).toFixed(1);
      const km = Math.round(radiusKm * f);
      return (
        `<circle class="radar-ring" cx="${CX}" cy="${CY}" r="${rr}"/>` +
        `<text class="radar-rlabel" x="${CX + 1.5}" y="${(CY + f * MAX_R - 1).toFixed(1)}">${km}</text>`
      );
    })
    .join("");

  const cross =
    `<line class="radar-cross" x1="${CX}" y1="${CY - MAX_R}" x2="${CX}" y2="${CY + MAX_R}"/>` +
    `<line class="radar-cross" x1="${CX - MAX_R}" y1="${CY}" x2="${CX + MAX_R}" y2="${CY}"/>`;

  const compass =
    `<text class="radar-compass" x="${CX}" y="10" text-anchor="middle">N</text>` +
    `<text class="radar-compass" x="${CX}" y="198" text-anchor="middle">S</text>` +
    `<text class="radar-compass" x="195" y="${CY + 3.5}" text-anchor="middle">E</text>` +
    `<text class="radar-compass" x="5" y="${CY + 3.5}" text-anchor="middle">W</text>`;

  // Slow rotating sweep wedge (30°), CSS-animated; respects reduced-motion.
  const tipX = (CX + MAX_R * Math.sin(Math.PI / 6)).toFixed(1);
  const tipY = (CY - MAX_R * Math.cos(Math.PI / 6)).toFixed(1);
  const sweep =
    `<g class="radar-sweep"><path d="M${CX} ${CY} L${CX} ${CY - MAX_R} ` +
    `A${MAX_R} ${MAX_R} 0 0 1 ${tipX} ${tipY} Z"/></g>`;

  const planes = aircraft
    .filter((a) => a.bearing_deg != null && a.distance_km != null)
    .map((a) => {
      const { x, y } = polar(a.distance_km, radiusKm, a.bearing_deg);
      const track = a.track ?? 0;
      const xs = x.toFixed(1);
      const ys = y.toFixed(1);
      return (
        `<g class="radar-plane ${altBand(a)}" data-detail="${escapeHtml(a.hex)}">` +
        `<circle class="radar-hit" cx="${xs}" cy="${ys}" r="9"/>` +
        `<g transform="translate(${xs} ${ys}) rotate(${track}) scale(${PLANE_SCALE})">` +
        `<path d="${AIRCRAFT_PATH}" transform="translate(-12 -12)"/></g>` +
        `</g>`
      );
    })
    .join("");

  return (
    `<svg class="radar-svg" viewBox="0 0 ${VIEW} ${VIEW}" preserveAspectRatio="xMidYMid meet">` +
    `<circle class="radar-face" cx="${CX}" cy="${CY}" r="${MAX_R}"/>` +
    sweep +
    rings +
    cross +
    compass +
    planes +
    `<circle class="radar-center" cx="${CX}" cy="${CY}" r="2"/>` +
    `</svg>`
  );
}

function listHtml(aircraft: any[], data: any): string {
  const rows = aircraft.length
    ? aircraft
        .slice(0, 10)
        .map(
          (a) => `<div class="adsb-row ${altBand(a)}" data-detail="${escapeHtml(a.hex)}">
            <span class="adsb-flight">${escapeHtml(a.flight)}</span>
            <span class="adsb-alt">${fmtAlt(a)}</span>
            <span class="adsb-speed">${a.speed_kt != null ? `${Math.round(a.speed_kt)} kt` : "—"}</span>
            <span class="adsb-dist">${a.distance_km} km ${escapeHtml(a.direction)}</span>
            <span class="adsb-track" style="transform: rotate(${a.track ?? 0}deg)">${AIRCRAFT_ICON}</span>
          </div>`,
        )
        .join("")
    : `<div class="empty">No aircraft within ${data?.radius_km ?? "?"} km</div>`;
  return `<div class="adsb-list">
      <div class="adsb-header">
        <span class="adsb-title">${AIRCRAFT_ICON}<span>overhead now</span></span>
        <span class="adsb-count">${data?.count_in_radius ?? 0} within ${data?.radius_km ?? "?"} km · ${data?.count_total ?? 0} tracked</span>
      </div>
      ${rows}
    </div>`;
}

function vrMarkup(vr: number | null | undefined): string {
  if (vr == null || Math.abs(vr) < 100) return "";
  const arrow = vr > 0 ? CLIMB : DESCEND;
  const cls = vr > 0 ? "vr-up" : "vr-down";
  return ` <span class="vr ${cls}">${arrow}${Math.abs(Math.round(vr)).toLocaleString()}</span>`;
}

function stat(label: string, value: string): string {
  return `<div class="adsb-stat"><span class="adsb-stat-l">${label}</span><span class="adsb-stat-v">${value}</span></div>`;
}

register({
  id: "adsb",
  renderStage(el, data) {
    const aircraft: any[] = data?.aircraft ?? [];
    const radiusKm: number = data?.radius_km ?? 40;
    el.innerHTML = `<div class="adsb-radar">
      <div class="radar-scope">${scopeSvg(aircraft, radiusKm)}</div>
      <div class="radar-side">${listHtml(aircraft, data)}</div>
    </div>`;
  },
  getDetailItem(stage, key) {
    const list: any[] = stage?.aircraft ?? [];
    return list.find((a) => a.hex === key) ?? null;
  },
  renderDetail(el, item: any) {
    if (!item) return;
    const track = item.track ?? 0;
    const subtitle = [item.registration, item.desc || item.type]
      .filter(Boolean)
      .join(" · ");
    el.innerHTML = `<div class="detail adsb-detail">
      <div class="adsb-detail-hero ${altBand(item)}">
        <span class="adsb-detail-plane" style="transform: rotate(${track}deg)">${AIRCRAFT_ICON}</span>
      </div>
      <div class="adsb-detail-main">
        <div class="adsb-detail-call">${escapeHtml(item.flight)}</div>
        ${subtitle ? `<div class="adsb-detail-sub">${escapeHtml(subtitle)}</div>` : ""}
        ${item.operator ? `<div class="adsb-detail-op">${escapeHtml(item.operator)}</div>` : ""}
        <div class="adsb-detail-stats">
          ${stat("Altitude", `${fmtAlt(item)}${vrMarkup(item.vert_rate)}`)}
          ${stat("Ground speed", item.speed_kt != null ? `${Math.round(item.speed_kt)} kt` : "—")}
          ${stat("Heading", item.track != null ? `${Math.round(item.track)}°` : "—")}
          ${stat("Range", `${item.distance_km} km ${escapeHtml(item.direction)}`)}
          ${stat("Bearing", item.bearing_deg != null ? `${Math.round(item.bearing_deg)}°` : "—")}
          ${stat("Squawk", item.squawk ? escapeHtml(item.squawk) : "—")}
        </div>
        ${item.on_ground ? `<div class="adsb-detail-badge">on ground</div>` : ""}
      </div>
    </div>`;
  },
});
