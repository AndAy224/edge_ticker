// NHC tropical tracker: storm positions, forecast track and cone drawn over
// the shared Carto dark slippy map, auto-fitted to home + every active storm.
// Off-season it renders a calm basin map with a "tropics quiet" chip.
// Reuses the radar module's map plumbing classes (.radar-viewport/.radar-map/
// .radar-basemap/.radar-home) — those are effectively the shared slippy-map
// styles; module-specific bits are .hurr-*.
import {
  CARTO_ATTRIB,
  MapView,
  basemapImgs,
  fitView,
  mapScaleStyle,
  project,
  tileZoom,
} from "../slippymap";
import { register } from "./registry";

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!,
  );
}

// Atlantic basin view used when no storms are active (Gulf + Caribbean + W Atlantic).
const QUIET_BASIN = [
  { lat: 9, lon: -98 },
  { lat: 33, lon: -48 },
];

function categoryColor(category: string): string {
  if (category === "CAT 5" || category === "CAT 4") return "#ff4d4d";
  if (category === "CAT 3") return "#ff8c42";
  if (category === "CAT 2" || category === "CAT 1") return "#ffb000";
  if (category === "TS") return "#ffd966";
  return "#9ecbff"; // TD / depression
}

/** Tropical-cyclone glyph (no emoji fonts on the device): filled core with
 *  two comma tails, drawn at pre-scale position (x, y). */
function stormIcon(x: number, y: number, color: string): string {
  return `<g transform="translate(${x.toFixed(1)},${y.toFixed(1)})" class="hurr-icon">
    <path d="M 5.5 -4.5 Q 14 -8 16 -17 M -5.5 4.5 Q -14 8 -16 17"
      fill="none" stroke="${color}" stroke-width="3.5" stroke-linecap="round"
      vector-effect="non-scaling-stroke"/>
    <circle r="7.5" fill="${color}"/>
    <circle r="3" fill="rgba(6,9,14,0.85)"/>
  </g>`;
}

function buildMap(stage: HTMLElement, data: any): void {
  const viewport = stage.querySelector<HTMLElement>(".radar-viewport");
  if (!viewport) return;
  const width = viewport.clientWidth;
  const height = viewport.clientHeight;
  if (!width || !height) return;

  const storms: any[] = data.storms ?? [];
  const home = data.home ?? { lat: 27.9659, lon: -82.8001 };

  const fitPoints = storms.length
    ? [
        home,
        ...storms.flatMap((s) => [
          { lat: s.lat, lon: s.lon },
          ...(s.track ?? []),
          ...(s.cone ?? []).map((c: number[]) => ({ lat: c[0], lon: c[1] })),
        ]),
      ]
    : QUIET_BASIN;
  const view: MapView = fitView(fitPoints, width, height, {
    minZoom: 2.5,
    maxZoom: 7,
  });
  const { scale } = tileZoom(view);

  // SVG layers live inside the scaled map container so they track the tiles;
  // strokes stay constant via vector-effect. Name chips render outside it
  // (post-scale coordinates) so text stays crisp and constant-size.
  let svg = "";
  let chips = "";
  const postScale = (p: { x: number; y: number }) => ({
    x: width / 2 + (p.x - width / 2) * scale,
    y: height / 2 + (p.y - height / 2) * scale,
  });
  for (const s of storms) {
    const color = categoryColor(s.category);
    const pos = project(view, s.lat, s.lon);
    const cone: number[][] = s.cone ?? [];
    if (cone.length >= 3) {
      const points = cone
        .map((c) => {
          const p = project(view, c[0], c[1]);
          return `${p.x.toFixed(1)},${p.y.toFixed(1)}`;
        })
        .join(" ");
      svg += `<polygon points="${points}" class="hurr-cone" vector-effect="non-scaling-stroke"/>`;
    }
    const track: any[] = s.track ?? [];
    if (track.length) {
      const path = [pos, ...track.map((t) => project(view, t.lat, t.lon))];
      svg += `<polyline points="${path
        .map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`)
        .join(" ")}" class="hurr-track" vector-effect="non-scaling-stroke"/>`;
      svg += path
        .slice(1)
        .map(
          (p) =>
            `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="3.5" class="hurr-track-dot" vector-effect="non-scaling-stroke"/>`,
        )
        .join("");
    }
    svg += stormIcon(pos.x, pos.y, color);
    const chip = postScale(pos);
    chips += `<div class="hurr-name" style="left:${chip.x.toFixed(1)}px;top:${(chip.y - 26).toFixed(1)}px;border-color:${color}">
      ${escapeHtml(s.category)} ${escapeHtml(s.name)}</div>`;
  }

  const homePos = postScale(project(view, home.lat, home.lon));
  const homeVisible =
    homePos.x > -20 && homePos.x < width + 20 && homePos.y > -20 && homePos.y < height + 20;

  viewport.innerHTML = `
    <div class="radar-map" style="${mapScaleStyle(view)}">
      <div class="radar-basemap">${basemapImgs(view)}</div>
      <svg class="hurr-overlay" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">${svg}</svg>
    </div>
    ${chips}
    ${
      homeVisible
        ? `<div class="radar-home" style="left:${homePos.x.toFixed(1)}px;top:${homePos.y.toFixed(1)}px;margin:-7px 0 0 -7px"></div>`
        : ""
    }
    ${data.quiet ? `<div class="hurr-quiet">No active Atlantic systems</div>` : ""}`;
}

// NHC omits a motion fix (null) on newly-formed systems, and reports 0 mph
// when a storm is stationary — mirror the collector's movement_phrase().
function movementPhrase(dir: unknown, mph: unknown): string {
  if (mph == null) return "movement TBD";
  if (mph === 0) return "stationary";
  return `moving ${escapeHtml(dir)} ${escapeHtml(mph)} mph`;
}

function stormCard(s: any): string {
  const color = categoryColor(s.category);
  return `<div class="hurr-card" style="border-color:${color}">
    <div class="hurr-card-head">
      <span class="hurr-cat" style="background:${color}">${escapeHtml(s.category)}</span>
      <span class="hurr-card-name">${escapeHtml(s.name)}</span>
    </div>
    <div class="hurr-card-stats">
      ${s.wind_mph} mph · ${escapeHtml(s.pressure_mb ?? "—")} mb ·
      ${movementPhrase(s.movement_dir, s.movement_mph)}
    </div>
    <div class="hurr-card-dist">${s.distance_mi} mi ${escapeHtml(s.bearing_from_home)} of ${escapeHtml(
      s.home_name ?? "home",
    )}</div>
  </div>`;
}

register({
  id: "hurricanes",
  renderStage(el, data) {
    const storms: any[] = (data?.storms ?? []).map((s: any) => ({
      ...s,
      home_name: (data?.location_name ?? "home").split(",")[0],
    }));
    el.innerHTML = `<div class="hurr-stage">
      <div class="radar-viewport"></div>
      <div class="hurr-cards">${storms.map(stormCard).join("")}</div>
      <div class="radar-attrib">${CARTO_ATTRIB} &middot; NOAA/NHC</div>
    </div>`;
    const stage = el.querySelector<HTMLElement>(".hurr-stage")!;
    requestAnimationFrame(() => {
      if (!el.isConnected) return;
      buildMap(stage, { ...data, storms });
    });
  },
});
