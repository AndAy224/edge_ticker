// Animated precipitation radar: RainViewer frame tiles looping over a Carto
// dark basemap, centered on the home location. The loop is pure CSS (generated
// keyframes, negative delays) so nothing needs teardown when the layer is
// removed — same reasoning as the ADS-B sweep animation.
import { register } from "./registry";

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!,
  );
}

const TILE_PX = 512; // on-screen size per slippy tile; RainViewer /512/ and Carto @2x render 1:1
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
  // Fractional zoom: tiles come from the nearest integer level and the whole
  // map layer is CSS-scaled about its center (where home sits) — rounding up
  // then downscaling keeps the basemap crisp and avoids per-tile seams.
  const zoom: number = data.zoom ?? 7.5;
  const z = Math.round(zoom);
  const scale = 2 ** (zoom - z);

  // Radar data stops at RADAR_MAX_Z — past that, keep the basemap at z and
  // upscale the (blobby anyway) radar tiles to match.
  const rz = Math.min(z, RADAR_MAX_Z);

  // Tile mosaic for one layer at tile-level lz, in pre-scale pixels.
  const layerImgs = (lz: number, src: (x: number, y: number) => string): string => {
    const ln = 2 ** lz;
    const tilePx = TILE_PX * 2 ** (z - lz);
    // Web-Mercator: home location in fractional tile coordinates at lz.
    const latRad = ((data.center?.lat ?? 0) * Math.PI) / 180;
    const xf = (((data.center?.lon ?? 0) + 180) / 360) * ln;
    const yf =
      ((1 - Math.log(Math.tan(latRad) + 1 / Math.cos(latRad)) / Math.PI) / 2) * ln;
    // Range must cover the viewport in pre-scale pixels.
    const halfW = width / 2 / scale;
    const halfH = height / 2 / scale;
    const x0 = Math.floor(xf - halfW / tilePx);
    const x1 = Math.floor(xf + halfW / tilePx);
    const y0 = Math.max(0, Math.floor(yf - halfH / tilePx));
    const y1 = Math.min(ln - 1, Math.floor(yf + halfH / tilePx));
    let imgs = "";
    for (let x = x0; x <= x1; x++) {
      for (let y = y0; y <= y1; y++) {
        const wx = ((x % ln) + ln) % ln; // wrap for tile URLs
        const left = Math.round((x - xf) * tilePx + width / 2);
        const top = Math.round((y - yf) * tilePx + height / 2);
        imgs +=
          `<img src="${src(wx, y)}" ` +
          `style="left:${left}px;top:${top}px;width:${tilePx}px;height:${tilePx}px" ` +
          `onerror="this.style.display='none'" alt="">`;
      }
    }
    return imgs;
  };

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
      layerImgs(rz, (x, y) => `${data.host}${f.path}/512/${rz}/${x}/${y}/${data.color ?? 4}/1_1.png`) +
      `</div>`;
    chips += `<span class="radar-time${f.nowcast ? " nowcast" : ""}${newest}" style="${anim}">${escapeHtml(
      frameLabel(f, newestPast),
    )}</span>`;
  }

  viewport.innerHTML = `
    <style>${css}</style>
    <div class="radar-map" style="transform:scale(${scale.toFixed(4)})">
      <div class="radar-basemap">${layerImgs(
        z,
        (x, y) => `https://basemaps.cartocdn.com/dark_nolabels/${z}/${x}/${y}@2x.png`,
      )}</div>
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
      <div class="radar-attrib">&copy; OpenStreetMap &copy; CARTO &middot; RainViewer</div>
    </div>`;
    // The layer is detached until crossfade() appends it — measure after attach.
    const stage = el.querySelector<HTMLElement>(".radar-stage")!;
    requestAnimationFrame(() => {
      if (!el.isConnected) return;
      buildMap(stage, { ...data, frames });
    });
  },
});
