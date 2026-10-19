// Ships: live AIS traffic around home (aisstream.io via the ships collector).
// Left, the shared Esri dark slippy map fitted to the range ring, with a hull
// per vessel under way (aimed along its heading, sized by length, coloured by
// type) and a dot per moored/anchored one; right, the nearest vessels under
// way, ADS-B style. Narrow panes keep the map and drop or trim the list.
// Reuses the radar/hurricane map plumbing (.radar-viewport/.radar-map/
// .radar-basemap/.radar-home/.radar-attrib/.hurr-quiet) and the ADS-B contact
// list and detail card classes (.adsb-list/.adsb-header/.adsb-row,
// .adsb-detail*), so the themes treat them alike; ship-specific bits are
// .ships-*.
import { BASEMAP_ATTRIB, MapView, basemapImgs, fitView, mapScaleStyle, project, tileZoom } from "../slippymap";
import { HULL_ICON, HULL_PATH, VESSEL_ICONS } from "../icons";
import { register } from "./registry";

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!,
  );
}

const EARTH_KM = 6371;
const CHIP_KINDS = new Set(["cargo", "tanker", "passenger", "patrol", "military"]);
const MAX_CHIPS = 4;

/** The point `km` from (lat, lon) along `bearing` — for the range ring. */
function destination(lat: number, lon: number, km: number, bearing: number) {
  const r = Math.PI / 180;
  const d = km / EARTH_KM;
  const p1 = lat * r;
  const b = bearing * r;
  const p2 = Math.asin(Math.sin(p1) * Math.cos(d) + Math.cos(p1) * Math.sin(d) * Math.cos(b));
  const l2 =
    lon * r +
    Math.atan2(Math.sin(b) * Math.sin(d) * Math.cos(p1), Math.cos(d) - Math.sin(p1) * Math.sin(p2));
  return { lat: p2 / r, lon: l2 / r };
}

/** Hull scale by length: a 10 m launch reads smaller than a 300 m tanker.
 *  The 18-unit hull × 1.4 ≈ 25 px for an unknown length. */
function hullScale(v: any): number {
  const length = v.length_m;
  const base = 1.4;
  if (length == null) return base;
  if (length < 15) return base * 0.8;
  if (length < 50) return base * 0.95;
  if (length < 150) return base * 1.15;
  return base * 1.35;
}

function aim(v: any): number {
  return v.heading ?? v.course ?? 0;
}

function speedText(v: any): string {
  if (v.moving) return `${Number(v.speed_kn).toFixed(v.speed_kn < 10 ? 1 : 0)} kn`;
  if (v.status === "moored" || v.status === "at anchor" || v.status === "aground") return v.status;
  return "stopped";
}

// "vessel" (unknown or unclassified type) adds nothing to a row or the tape.
const GENERIC_KINDS = new Set(["unknown", "other"]);

function kindText(v: any): string {
  const parts = [GENERIC_KINDS.has(v.kind) ? "" : v.kind_label];
  if (v.destination) parts.push(`→ ${v.destination}`);
  return parts.filter(Boolean).join(" ");
}

function rowMark(v: any): string {
  return v.moving
    ? `<span class="ships-mark" style="transform: rotate(${aim(v)}deg)">${HULL_ICON}</span>`
    : `<span class="ships-mark"><span class="ships-dot"></span></span>`;
}

function countLine(data: any): string {
  const within = data?.count_in_radius ?? 0;
  const moving = data?.count_moving ?? 0;
  const tail = data?.warming ? " · filling in" : "";
  return `${moving} under way · ${within - moving} stopped within ${data?.radius_km ?? "?"} km${tail}`;
}

function listHtml(vessels: any[], data: any): string {
  const rows = vessels.length
    ? vessels
        // 7 rows + header fit the 1-pane budget (the ADS-B list's cap);
        // narrower panes are capped further in CSS via [data-panes] nth-child.
        .slice(0, 7)
        .map(
          (v) => `<div class="adsb-row ships-row k-${escapeHtml(v.kind)}" data-detail="${escapeHtml(v.mmsi)}">
            ${rowMark(v)}
            <span class="ships-name">${escapeHtml(v.name)}</span>
            <span class="ships-kind">${escapeHtml(kindText(v))}</span>
            <span class="ships-speed">${escapeHtml(speedText(v))}</span>
            <span class="ships-dist">${v.distance_km} km ${escapeHtml(v.direction)}</span>
          </div>`,
        )
        .join("")
    : `<div class="empty">${
        data?.warming ? "Listening for ships…" : `No vessels within ${data?.radius_km ?? "?"} km`
      }</div>`;
  return `<div class="adsb-list">
      <div class="adsb-header">
        <span class="adsb-title">${VESSEL_ICONS.ship}<span>on the water</span></span>
        <span class="adsb-count">${countLine(data)}</span>
      </div>
      ${rows}
    </div>`;
}

function buildMap(stage: HTMLElement, data: any): void {
  const viewport = stage.querySelector<HTMLElement>(".radar-viewport");
  const center = data?.center;
  if (!viewport || !center || typeof center.lat !== "number") return;
  const width = viewport.clientWidth;
  const height = viewport.clientHeight;
  if (!width || !height) return;
  const radius: number = data.radius_km ?? 50;

  const ring = [0, 90, 180, 270].map((b) => destination(center.lat, center.lon, radius, b));
  const view: MapView = fitView(ring, width, height, { minZoom: 5, maxZoom: 13, margin: 0.92 });
  const { scale } = tileZoom(view);
  // Markers and chips go outside the scaled map container, at post-scale
  // coordinates, so hulls and text stay a constant size at fractional zooms.
  const at = (lat: number, lon: number) => {
    const p = project(view, lat, lon);
    return { x: width / 2 + (p.x - width / 2) * scale, y: height / 2 + (p.y - height / 2) * scale };
  };

  const ringPts: string[] = [];
  for (let b = 0; b <= 360; b += 5) {
    const d = destination(center.lat, center.lon, radius, b);
    const p = at(d.lat, d.lon);
    ringPts.push(`${p.x.toFixed(1)},${p.y.toFixed(1)}`);
  }

  const vessels: any[] = data.vessels ?? [];
  // Stopped vessels first so the ones under way draw on top of the marinas.
  const ordered = [...vessels].sort((a, b) => Number(a.moving) - Number(b.moving));
  let marks = "";
  for (const v of ordered) {
    const p = at(v.lat, v.lon);
    const shape = v.moving
      ? `<path d="${HULL_PATH}" transform="rotate(${aim(v)}) scale(${hullScale(v)}) translate(-12 -12)"/>`
      : `<circle class="ships-dot-mark" r="5.5"/>`;
    marks +=
      `<g class="ships-marker k-${escapeHtml(v.kind)}" data-detail="${escapeHtml(v.mmsi)}" ` +
      `transform="translate(${p.x.toFixed(1)} ${p.y.toFixed(1)})">` +
      `<circle class="radar-hit" r="16"/>${shape}</g>`;
  }

  // Names for the few big ships under way, skipping any that would collide.
  let chips = "";
  const placed: { x: number; y: number }[] = [];
  for (const v of vessels) {
    if (placed.length >= MAX_CHIPS) break;
    if (!v.moving || !CHIP_KINDS.has(v.kind)) continue;
    const p = at(v.lat, v.lon);
    if (placed.some((q) => Math.abs(q.x - p.x) < 190 && Math.abs(q.y - p.y) < 34)) continue;
    placed.push(p);
    chips += `<div class="ships-chip k-${escapeHtml(v.kind)}" style="left:${p.x.toFixed(1)}px;top:${(p.y - 16).toFixed(1)}px">${escapeHtml(v.name)}</div>`;
  }

  const home = at(center.lat, center.lon);
  viewport.innerHTML = `
    <div class="radar-map" style="${mapScaleStyle(view)}">
      <div class="radar-basemap">${basemapImgs(view)}</div>
    </div>
    <svg class="ships-overlay" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">
      <polyline class="ships-ring" points="${ringPts.join(" ")}"/>
      ${marks}
    </svg>
    ${chips}
    <div class="radar-home" style="left:${home.x.toFixed(1)}px;top:${home.y.toFixed(1)}px"></div>`;
}

function etaText(iso: string | null | undefined): string {
  if (!iso) return "";
  const eta = new Date(iso);
  if (isNaN(eta.getTime())) return "";
  const time = eta.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const days = (eta.getTime() - Date.now()) / 86_400_000;
  if (eta.toDateString() === new Date().toDateString()) return `ETA ${time}`;
  if (days > -1 && days < 6) return `ETA ${eta.toLocaleDateString([], { weekday: "short" })} ${time}`;
  return `ETA ${eta.toLocaleDateString([], { month: "short", day: "numeric" })} ${time}`;
}

function stat(label: string, value: string): string {
  return `<div class="adsb-stat"><span class="adsb-stat-l">${label}</span><span class="adsb-stat-v">${value}</span></div>`;
}

register({
  id: "ships",
  renderStage(el, data) {
    const vessels: any[] = data?.vessels ?? [];
    el.innerHTML = `<div class="ships-stage">
      <div class="ships-map">
        <div class="radar-viewport"></div>
        ${data?.warming && !vessels.length ? `<div class="hurr-quiet">Listening for ships…</div>` : ""}
        <div class="ships-count-chip">${escapeHtml(countLine(data))}</div>
        <div class="radar-attrib">${BASEMAP_ATTRIB} &middot; AIS: aisstream.io</div>
      </div>
      <div class="ships-side">${listHtml(vessels, data)}</div>
    </div>`;
    const stage = el.querySelector<HTMLElement>(".ships-stage")!;
    requestAnimationFrame(() => {
      if (!el.isConnected) return;
      buildMap(stage, data);
    });
  },
  getDetailItem(stage, key) {
    const list: any[] = stage?.vessels ?? [];
    return list.find((v) => v.mmsi === key) ?? null;
  },
  renderDetail(el, item: any) {
    if (!item) return;
    const sub = [
      item.kind_label,
      item.country,
      item.imo ? `IMO ${item.imo}` : "",
    ]
      .filter(Boolean)
      .join(" · ");
    const ids = [
      item.callsign ? `call sign ${item.callsign}` : "",
      `MMSI ${item.mmsi}`,
      item.ais_class ? `AIS class ${item.ais_class}` : "",
    ]
      .filter(Boolean)
      .join(" · ");
    const voyage = [item.destination ? `→ ${item.destination}` : "", etaText(item.eta)]
      .filter(Boolean)
      .join(" · ");
    const size =
      item.length_m != null
        ? `${item.length_m}${item.beam_m != null ? ` × ${item.beam_m}` : ""} m`
        : "—";
    el.innerHTML = `<div class="detail adsb-detail ships-detail">
      <div class="adsb-detail-hero ships-hero k-${escapeHtml(item.kind)}">
        ${
          item.moving
            ? `<span class="adsb-detail-plane" style="transform: rotate(${aim(item)}deg)">${HULL_ICON}</span>`
            : `<span class="adsb-detail-plane">${VESSEL_ICONS.ship}</span>`
        }
      </div>
      <div class="adsb-detail-main">
        <div class="adsb-detail-call">${escapeHtml(item.name)}</div>
        <div class="adsb-detail-sub">${escapeHtml(sub)}</div>
        <div class="adsb-detail-op">${escapeHtml(ids)}</div>
        ${voyage ? `<div class="ships-voyage">${escapeHtml(voyage)}</div>` : ""}
        <div class="adsb-detail-stats">
          ${stat("Speed", item.speed_kn != null ? `${item.speed_kn} kn` : "—")}
          ${stat("Course", item.course != null ? `${item.course}°` : "—")}
          ${stat("Status", escapeHtml(item.status ?? (item.moving ? "under way" : "—")))}
          ${stat("Range", `${item.distance_km} km ${escapeHtml(item.direction)}`)}
          ${stat("Size", size)}
          ${stat("Draught", item.draught_m != null ? `${item.draught_m} m` : "—")}
        </div>
      </div>
    </div>`;
  },
});
