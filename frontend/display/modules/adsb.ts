import { register } from "./registry";
import { AIRCRAFT_ICON, AIRCRAFT_PATH } from "../icons";
import { COASTLINE, AIRPORTS, PLACES } from "./basemap-tampa";

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

// --- geographic basemap (coastline/airports), projected onto the scope ---
// Same azimuthal mapping as the planes (bearing + distance from the receiver),
// so the coast lines up with the aircraft. Mirrors the backend's haversine /
// bearing so a point computed here lands where the collector would place it.
const EARTH_KM = 6371;

function geoDistanceKm(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const r = Math.PI / 180;
  const p1 = lat1 * r;
  const p2 = lat2 * r;
  const dp = (lat2 - lat1) * r;
  const dl = (lon2 - lon1) * r;
  const a =
    Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return 2 * EARTH_KM * Math.asin(Math.sqrt(a));
}

function geoBearing(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const r = Math.PI / 180;
  const p1 = lat1 * r;
  const p2 = lat2 * r;
  const dl = (lon2 - lon1) * r;
  const x = Math.sin(dl) * Math.cos(p2);
  const y = Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl);
  return ((Math.atan2(x, y) * 180) / Math.PI + 360) % 360;
}

// Project lat/lon to scope coords WITHOUT clamping (the scope clipPath trims
// anything past the rim, so coast lines crossing the edge stay straight).
function projectGeo(lat: number, lon: number, center: any, radiusKm: number) {
  const dist = geoDistanceKm(center.lat, center.lon, lat, lon);
  const rad = (geoBearing(center.lat, center.lon, lat, lon) * Math.PI) / 180;
  const r = (radiusKm > 0 ? dist / radiusKm : 0) * MAX_R;
  return { x: CX + r * Math.sin(rad), y: CY - r * Math.cos(rad), dist };
}

// Building the basemap projects ~2.3k points, so cache it by center+radius
// (it only changes when the receiver location or range does).
let basemapCache: { key: string; svg: string } | null = null;

function basemapSvg(center: any, radiusKm: number): string {
  if (!center || typeof center.lat !== "number") return "";
  const key = `${center.lat},${center.lon},${radiusKm}`;
  if (basemapCache && basemapCache.key === key) return basemapCache.svg;

  let coast = "";
  for (const way of COASTLINE) {
    let pts = "";
    let near = false;
    for (const p of way) {
      const { x, y, dist } = projectGeo(p[1], p[0], center, radiusKm);
      pts += `${x.toFixed(1)},${y.toFixed(1)} `;
      if (dist <= radiusKm * 1.5) near = true;
    }
    if (near) coast += `<polyline class="coast-line" points="${pts.trim()}"/>`;
  }

  let places = "";
  for (const pl of PLACES) {
    const { x, y, dist } = projectGeo(pl.lat, pl.lon, center, radiusKm);
    if (dist > radiusKm * 0.96) continue;
    places += `<text class="coast-place" x="${x.toFixed(1)}" y="${y.toFixed(1)}" text-anchor="middle">${escapeHtml(pl.label)}</text>`;
  }

  let airports = "";
  for (const a of AIRPORTS) {
    const { x, y, dist } = projectGeo(a.lat, a.lon, center, radiusKm);
    if (dist > radiusKm) continue;
    airports +=
      `<g class="coast-airport">` +
      `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="2"/>` +
      `<text x="${x.toFixed(1)}" y="${(y - 3.4).toFixed(1)}" text-anchor="middle">${escapeHtml(a.label)}</text>` +
      `</g>`;
  }

  const svg = `<g class="radar-basemap" clip-path="url(#radar-clip)">${coast}${places}${airports}</g>`;
  basemapCache = { key, svg };
  return svg;
}

function scopeSvg(aircraft: any[], radiusKm: number, center: any): string {
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
    `<defs><clipPath id="radar-clip"><circle cx="${CX}" cy="${CY}" r="${MAX_R}"/></clipPath></defs>` +
    `<circle class="radar-face" cx="${CX}" cy="${CY}" r="${MAX_R}"/>` +
    basemapSvg(center, radiusKm) +
    sweep +
    rings +
    cross +
    compass +
    planes +
    `<circle class="radar-center" cx="${CX}" cy="${CY}" r="2"/>` +
    `<text class="radar-attrib" x="${VIEW - 2}" y="${VIEW - 2}" text-anchor="end">© OpenStreetMap</text>` +
    `</svg>`
  );
}

function listHtml(aircraft: any[], data: any): string {
  const rows = aircraft.length
    ? aircraft
        // 7 rows + header fit the 528px 1-pane budget; narrower panes are
        // capped further in CSS via [data-panes] nth-child.
        .slice(0, 7)
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

// Origin → destination arrow (no emoji fonts on the device → inline SVG).
const ROUTE_ARROW =
  `<svg class="rt-arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" ` +
  `stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">` +
  `<path d="M4 12h15M13 6l6 6-6 6"/></svg>`;

function airportHtml(ap: any): string {
  if (!ap) return `<span class="rt-end rt-unknown"><span class="rt-code">—</span></span>`;
  const code = ap.iata || ap.icao || "—";
  const city = ap.city || ap.name || "";
  return `<span class="rt-end">
    <span class="rt-code">${escapeHtml(code)}</span>
    ${city ? `<span class="rt-city">${escapeHtml(city)}</span>` : ""}
  </span>`;
}

// ADS-B carries no origin/destination — look the callsign up on tap and patch
// it into the readout (render base synchronously → fetch → patch if connected).
function enrichRoute(el: HTMLElement, item: any): void {
  const slot = el.querySelector(".adsb-detail-route") as HTMLElement | null;
  if (!slot) return;
  const callsign = String(item.flight ?? "").trim();
  // No real callsign (collector fell back to registration/hex) → no route to find.
  if (!callsign || callsign === item.registration || callsign === item.hex) return;
  fetch(`/api/adsb/route?callsign=${encodeURIComponent(callsign)}`)
    .then((r) => (r.ok ? r.json() : null))
    .then((d) => {
      const route = d?.route;
      if (!route || !el.isConnected || !slot.isConnected) return;
      if (!route.origin && !route.destination) return;
      slot.innerHTML = `<div class="adsb-route-line">
        ${airportHtml(route.origin)}
        <span class="rt-sep">${ROUTE_ARROW}</span>
        ${airportHtml(route.destination)}
      </div>`;
      slot.classList.add("filled");
    })
    .catch(() => {});
}

register({
  id: "adsb",
  renderStage(el, data) {
    const aircraft: any[] = data?.aircraft ?? [];
    const radiusKm: number = data?.radius_km ?? 40;
    el.innerHTML = `<div class="adsb-radar">
      <div class="radar-scope">${scopeSvg(aircraft, radiusKm, data?.center)}</div>
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
        <div class="adsb-detail-route"></div>
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
    enrichRoute(el, item);
  },
});
