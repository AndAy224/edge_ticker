// Animated precipitation radar: RainViewer frame tiles looping over a Carto
// dark basemap, centered on the home location. The loop is pure CSS (generated
// keyframes, negative delays) so nothing needs teardown when the layer is
// removed — same reasoning as the ADS-B sweep animation.
import {
  CARTO_ATTRIB,
  MapView,
  basemapImgs,
  layerImgs,
  mapScaleStyle,
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

const RADAR_MAX_Z = 7; // RainViewer serves "Zoom Level Not Supported" tiles above this
const STEP_SECONDS = 0.4; // per-frame dwell
const NEWEST_DWELL_STEPS = 4; // hold on the latest observed frame
const NOWCAST_END_DWELL_STEPS = 2; // brief pause on the last forecast frame

interface RadarFrame {
  time: number;
  path: string;
  nowcast: boolean;
}

function frameLabel(frame: RadarFrame, newest: RadarFrame): string {
  if (!frame.nowcast) {
    return new Date(frame.time * 1000).toLocaleTimeString([], {
      hour: "numeric",
      minute: "2-digit",
    });
  }
  const mins = Math.round((frame.time - newest.time) / 60);
  return `+${mins} MIN`;
}

/** Build the tile mosaic + per-frame animation once the layer is in the DOM
 *  and has real dimensions. */
function buildMap(stage: HTMLElement, data: any): void {
  const viewport = stage.querySelector<HTMLElement>(".radar-viewport");
  if (!viewport) return;
  const width = viewport.clientWidth;
  const height = viewport.clientHeight;
  if (!width || !height) return;

  const frames: RadarFrame[] = data.frames;
  const view: MapView = {
    lat: data.center?.lat ?? 0,
    lon: data.center?.lon ?? 0,
    zoom: data.zoom ?? 7.5,
    width,
    height,
  };
  const { z } = tileZoom(view);
  // Radar data stops at RADAR_MAX_Z — past that, keep the basemap at z and
  // upscale the (blobby anyway) radar tiles to match.
  const rz = Math.min(z, RADAR_MAX_Z);

  // Animation slots: 1 step per frame, extra dwell on the newest observation
  // and (when present) the last nowcast frame.
  const past = frames.filter((f) => !f.nowcast);
  const newestPast = past[past.length - 1] ?? frames[frames.length - 1];
  const slots = frames.map((f) => {
    if (f === newestPast) return NEWEST_DWELL_STEPS;
    if (f.nowcast && f === frames[frames.length - 1]) return NOWCAST_END_DWELL_STEPS;
    return 1;
  });
  const totalSteps = slots.reduce((a, b) => a + b, 0);
  const totalSeconds = totalSteps * STEP_SECONDS;

  // One @keyframes per distinct slot width, suffixed with the step total so a
  // crossfading old layer with a different frame count can't redefine ours.
  const uid = `${totalSteps}`;
  const widths = [...new Set(slots)];
  const css = widths
    .map((w) => {
      const pct = (w / totalSteps) * 100;
      return (
        `@keyframes radar-win-${w}-${uid}` +
        `{0%,${pct.toFixed(3)}%{opacity:1}${(pct + 0.001).toFixed(3)}%,100%{opacity:0}}`
      );
    })
    .join("\n");

  // Frames (tiles, scaled with the map) and timestamp chips (unscaled overlay)
  // are separate elements sharing the same animation, so they stay in sync.
  let start = 0;
  let frameDivs = "";
  let chips = "";
  for (let i = 0; i < frames.length; i++) {
    const f = frames[i];
    const delay = start * STEP_SECONDS - totalSeconds; // negative: loop already in phase
    start += slots[i];
    const anim =
      `animation:radar-win-${slots[i]}-${uid} ${totalSeconds}s linear ${delay}s infinite`;
    const newest = f === newestPast ? " newest" : "";
    frameDivs +=
      `<div class="radar-frame${newest}" style="${anim}">` +
      layerImgs(view, rz, (x, y) => `${data.host}${f.path}/512/${rz}/${x}/${y}/${data.color ?? 4}/1_1.png`) +
      `</div>`;
    chips += `<span class="radar-time${f.nowcast ? " nowcast" : ""}${newest}" style="${anim}">${escapeHtml(
      frameLabel(f, newestPast),
    )}</span>`;
  }

  viewport.innerHTML = `
    <style>${css}</style>
    <div class="radar-map" style="${mapScaleStyle(view)}">
      <div class="radar-basemap">${basemapImgs(view)}</div>
      ${frameDivs}
    </div>
    ${chips}
    <div class="radar-home"></div>`;
}

register({
  id: "weather_radar",
  renderStage(el, data) {
    const frames: RadarFrame[] = (data?.frames ?? []).filter(
      (f: any) => f?.time && f?.path,
    );
    if (!frames.length || !data?.host) {
      el.innerHTML = `<div class="empty">Radar unavailable</div>`;
      return;
    }
    el.innerHTML = `<div class="radar-stage">
      <div class="radar-viewport"></div>
      <div class="radar-loc">${escapeHtml(data.location_name ?? "")}</div>
      <div class="radar-attrib">${CARTO_ATTRIB} &middot; RainViewer</div>
    </div>`;
    // The layer is detached until crossfade() appends it — measure after attach.
    const stage = el.querySelector<HTMLElement>(".radar-stage")!;
    requestAnimationFrame(() => {
      if (!el.isConnected) return;
      buildMap(stage, { ...data, frames });
    });
  },
});
