// Shared slippy-map helpers for map-based stage modules (weather radar,
// hurricanes): Web-Mercator projection and absolutely-positioned tile
// mosaics, with fractional zoom support — tiles come from the nearest
// integer level and the map container is CSS-scaled about its center
// (which the modules position over the view center) for the remainder.

export const TILE_PX = 512; // on-screen px per slippy tile; RainViewer /512/ renders 1:1

// Esri World Dark Gray Base: keyless 256px tiles, requested one level deeper
// than the slippy level so four of them fill a 512px tile at the same
// resolution Carto's @2x tiles gave. (Carto began watermarking keyless tiles
// with "API KEY REQUIRED" in 2026.) Esri's path order is {z}/{y}/{x}.
const ESRI_DARK_URL =
  "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile";
const ESRI_MAX_Z = 16;

export const BASEMAP_ATTRIB = "&copy; Esri, HERE, Garmin, OpenStreetMap";

export interface MapView {
  lat: number; // view center
  lon: number;
  zoom: number; // fractional ok
  width: number; // viewport px
  height: number;
}

// Layers whose map is still to be built, with the frame callback that will do it.
const pendingBuilds = new Map<HTMLElement, number>();

/** Run `build` on the next frame if `layer` is in the document by then. A
 *  renderer is handed a detached layer and can only measure it once main.ts
 *  has attached it, hence the frame.
 *
 *  When no frames come (stalled compositor, hidden tab) every refresh would
 *  otherwise leave one more callback queued, each holding a layer and its
 *  payload long after a newer refresh replaced it — and all of them would run
 *  in the first frame back. So a layer that has left the document gives its
 *  callback up here, at the next render. The layer being rendered right now is
 *  not attached yet either, which is why it is registered after the sweep. */
export function buildWhenAttached(layer: HTMLElement, build: () => void): void {
  for (const [other, frame] of pendingBuilds) {
    if (!other.isConnected) {
      cancelAnimationFrame(frame);
      pendingBuilds.delete(other);
    }
  }
  pendingBuilds.set(
    layer,
    requestAnimationFrame(() => {
      pendingBuilds.delete(layer);
      if (layer.isConnected) build();
    }),
  );
}

/** Integer tile zoom + the residual CSS scale for a fractional view zoom.
 *  Rounding (not flooring) means the residual downscales more often than it
 *  upscales, which keeps tiles crisp. */
export function tileZoom(view: MapView): { z: number; scale: number } {
  const z = Math.round(view.zoom);
  return { z, scale: 2 ** (view.zoom - z) };
}

/** Fractional tile coordinates of a point at integer zoom lz. */
export function tileCoords(lat: number, lon: number, lz: number): { x: number; y: number } {
  const n = 2 ** lz;
  const latRad = (lat * Math.PI) / 180;
  return {
    x: ((lon + 180) / 360) * n,
    y: ((1 - Math.log(Math.tan(latRad) + 1 / Math.cos(latRad)) / Math.PI) / 2) * n,
  };
}

/** Pre-scale pixel position of a point within the view — the same coordinate
 *  space as the tile mosaic, valid inside the scaled map container. */
export function project(view: MapView, lat: number, lon: number): { x: number; y: number } {
  const { z } = tileZoom(view);
  const c = tileCoords(view.lat, view.lon, z);
  const p = tileCoords(lat, lon, z);
  return {
    x: (p.x - c.x) * TILE_PX + view.width / 2,
    y: (p.y - c.y) * TILE_PX + view.height / 2,
  };
}

/** Pick a centered view that fits all points with some margin. */
export function fitView(
  points: { lat: number; lon: number }[],
  width: number,
  height: number,
  opts: { minZoom: number; maxZoom: number; margin?: number },
): MapView {
  const margin = opts.margin ?? 0.75; // fraction of the pane the bbox may fill
  const merc = points.map((p) => tileCoords(p.lat, p.lon, 0));
  const xs = merc.map((m) => m.x);
  const ys = merc.map((m) => m.y);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  // Center back-projected from mercator midpoint (lat from inverse Gudermannian).
  const cx = (minX + maxX) / 2;
  const cy = (minY + maxY) / 2;
  const lon = cx * 360 - 180;
  const lat = (Math.atan(Math.sinh(Math.PI * (1 - 2 * cy))) * 180) / Math.PI;
  const spanX = Math.max(maxX - minX, 1e-6);
  const spanY = Math.max(maxY - minY, 1e-6);
  const zoom = Math.min(
    Math.log2((width * margin) / (spanX * TILE_PX)),
    Math.log2((height * margin) / (spanY * TILE_PX)),
  );
  return {
    lat,
    lon,
    zoom: Math.min(opts.maxZoom, Math.max(opts.minZoom, zoom)),
    width,
    height,
  };
}

/** Tile mosaic <img> markup for one layer at tile-level lz, in pre-scale px. */
export function layerImgs(
  view: MapView,
  lz: number,
  src: (x: number, y: number) => string,
): string {
  const { z, scale } = tileZoom(view);
  const ln = 2 ** lz;
  const tilePx = TILE_PX * 2 ** (z - lz);
  const { x: xf, y: yf } = tileCoords(view.lat, view.lon, lz);
  // Range must cover the viewport in pre-scale pixels.
  const halfW = view.width / 2 / scale;
  const halfH = view.height / 2 / scale;
  const x0 = Math.floor(xf - halfW / tilePx);
  const x1 = Math.floor(xf + halfW / tilePx);
  const y0 = Math.max(0, Math.floor(yf - halfH / tilePx));
  const y1 = Math.min(ln - 1, Math.floor(yf + halfH / tilePx));
  let imgs = "";
  for (let x = x0; x <= x1; x++) {
    for (let y = y0; y <= y1; y++) {
      const wx = ((x % ln) + ln) % ln; // wrap for tile URLs
      const left = Math.round((x - xf) * tilePx + view.width / 2);
      const top = Math.round((y - yf) * tilePx + view.height / 2);
      imgs +=
        `<img src="${src(wx, y)}" ` +
        `style="left:${left}px;top:${top}px;width:${tilePx}px;height:${tilePx}px" ` +
        `onerror="this.style.display='none'" alt="">`;
    }
  }
  return imgs;
}

/** Esri dark basemap mosaic (shared look for all map modules). */
export function basemapImgs(view: MapView): string {
  const { z } = tileZoom(view);
  const lz = Math.min(z + 1, ESRI_MAX_Z);
  return layerImgs(view, lz, (x, y) => `${ESRI_DARK_URL}/${lz}/${y}/${x}`);
}

/** Inline style applying the fractional-zoom residual to the map container. */
export function mapScaleStyle(view: MapView): string {
  const { scale } = tileZoom(view);
  return `transform:scale(${scale.toFixed(4)})`;
}
