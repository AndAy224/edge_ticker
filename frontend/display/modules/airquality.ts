import { AQI_ICON } from "../icons";
import { register } from "./registry";

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!,
  );
}

function hourLabel(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "numeric" });
}

// US-AQI bands — mirrors AQI_BANDS in backend/collectors/airquality.py. The
// backend's last band uses a 10 000 sentinel; the gauge clamps at the 500
// scale max instead.
const BANDS = [
  { hi: 50, key: "good", short: "Good" },
  { hi: 100, key: "moderate", short: "Moder." },
  { hi: 150, key: "usg", short: "USG" },
  { hi: 200, key: "unhealthy", short: "Unhlth" },
  { hi: 300, key: "very", short: "V.Unhl" },
  { hi: 500, key: "hazard", short: "Hazard" },
];

function bandIndex(value: number): number {
  const i = BANDS.findIndex((b) => value <= b.hi);
  return i === -1 ? BANDS.length - 1 : i;
}

function bandFloor(index: number): number {
  return index === 0 ? 0 : BANDS[index - 1].hi;
}

/** Position along the gauge, in percent. Bands are drawn equal-width rather
 *  than to a linear 0-500 scale — otherwise "Good" is a tenth of the bar and
 *  every ordinary day pins to the far left. */
function bandPos(value: number): number {
  const idx = bandIndex(value);
  const lo = bandFloor(idx);
  const hi = BANDS[idx].hi;
  const within = hi > lo ? Math.min(1, Math.max(0, (value - lo) / (hi - lo))) : 0;
  return ((idx + within) / BANDS.length) * 100;
}

function bandGauge(value: number): string {
  const active = bandIndex(value);
  const segs = BANDS.map(
    (b, i) =>
      `<span class="aq-band-seg" data-band="${b.key}"${i === active ? " data-active" : ""}></span>`,
  ).join("");
  const ticks = [0, ...BANDS.map((b) => b.hi)]
    .map((t) => `<span>${t}</span>`)
    .join("");
  const names = BANDS.map((b) => `<span>${b.short}</span>`).join("");
  return `<div class="aq-band">
    <div class="aq-band-track">
      ${segs}
      <span class="aq-band-marker" style="left:${bandPos(value).toFixed(2)}%"></span>
    </div>
    <div class="aq-band-ticks">${ticks}</div>
    <div class="aq-band-names">${names}</div>
  </div>`;
}

function concentration(value: number): string {
  // Sub-1 readings (SO₂ is routinely 0.4) must not round away to a bare "0".
  return value < 10 ? Number(value).toFixed(1) : String(Math.round(value));
}

/** One pollutant card. The bar is relative to the worst pollutant on the page
 *  (they are rank-ordered), so it reads as "share of the blame"; the sub-AQI
 *  number beside it carries the absolute value. */
function pollutantCard(p: any, maxSub: number, dominant: boolean): string {
  const sub = p.sub_aqi;
  const key = BANDS[bandIndex(sub ?? 0)].key;
  const width = sub != null && maxSub > 0 ? (sub / maxSub) * 100 : 0;
  return `<div class="aq-pollutant" data-band="${key}"${dominant ? " data-dominant" : ""}>
    <div class="aq-pollutant-head">
      <span class="aq-pollutant-label">${escapeHtml(p.label)}</span>
      ${sub != null ? `<span class="aq-pollutant-sub">${Math.round(sub)}</span>` : ""}
    </div>
    <div class="aq-pollutant-bar"><span style="width:${width.toFixed(1)}%"></span></div>
    <div class="aq-pollutant-value">${concentration(p.value)}<span>${escapeHtml(p.unit)}</span></div>
  </div>`;
}

const GRAPH_W = 1000;
const GRAPH_H = 160;
const GRAPH_PAD = 14;

/** AQI trend, scaled to the enclosing AQI bands rather than to min..max — a
 *  42→48 walk inside "Good" has to read as flat, not as a dramatic climb. */
function trendGraph(hours: any[]): string {
  const values = hours.map((h) => h.aqi).filter((v: any) => v != null);
  if (values.length < 2) return "";
  const lo = bandFloor(bandIndex(Math.min(...values)));
  const hi = BANDS[bandIndex(Math.max(...values))].hi;
  const span = hi - lo || 1;
  const x = (i: number) => (i / (hours.length - 1)) * GRAPH_W;
  const y = (v: number) =>
    GRAPH_PAD + (1 - (v - lo) / span) * (GRAPH_H - GRAPH_PAD * 2);
  const points = hours
    .map((h, i) => `${x(i).toFixed(1)},${y(h.aqi ?? lo).toFixed(1)}`)
    .join(" ");
  // No <text> in here: the svg stretches with preserveAspectRatio="none", which
  // squashes glyphs (same reason weather.ts overlays its labels as HTML).
  return `<svg class="aq-graph" viewBox="0 0 ${GRAPH_W} ${GRAPH_H}" preserveAspectRatio="none">
    <defs><linearGradient id="aq-grad" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" class="aq-fill-top"/>
      <stop offset="100%" class="aq-fill-bottom"/>
    </linearGradient></defs>
    <polygon fill="url(#aq-grad)" stroke="none"
      points="0,${GRAPH_H} ${points} ${GRAPH_W},${GRAPH_H}"/>
    <polyline class="aq-graph-line" points="${points}"/>
  </svg>`;
}

/** Hour ticks positioned at their true x%, not distributed by space-between —
 *  the curve's points are at i/(n-1), which space-between only matches when the
 *  series length happens to line up. */
function hourTicks(hours: any[]): string {
  const last = hours.length - 1;
  const picked = hours.map((_, i) => i).filter((i) => i % 3 === 0);
  // The final hour is worth labelling, but only when it won't crowd the tick
  // before it (the head already spells out the endpoints).
  if (last > 0 && last - picked[picked.length - 1] >= 3) picked.push(last);
  return picked
    .map((i) => {
      const pct = Math.min(97, Math.max(3, (i / last) * 100));
      return `<span style="left:${pct.toFixed(1)}%">${hourLabel(hours[i].time)}</span>`;
    })
    .join("");
}

register({
  id: "airquality",
  renderStage(el, data) {
    const aqi = data?.aqi;
    if (!aqi || aqi.value == null) {
      el.innerHTML = `<div class="empty">Waiting for air-quality data…</div>`;
      return;
    }
    const value = aqi.value;
    // Rank by the per-pollutant US-AQI sub-index the collector already ships, so
    // the worst contributor leads and narrow panes can cap the tail.
    const pollutants: any[] = [...(data?.pollutants ?? [])].sort(
      (a, b) => (b.sub_aqi ?? -1) - (a.sub_aqi ?? -1),
    );
    const maxSub = Math.max(0, ...pollutants.map((p) => p.sub_aqi ?? 0));
    const pollen: any[] = (data?.pollen ?? []).slice(0, 8);
    const hours: any[] = data?.hourly ?? [];
    const series = hours.map((h) => h.aqi).filter((v: any) => v != null);
    const trend =
      series.length >= 2
        ? `${Math.round(series[0])} → ${Math.round(series[series.length - 1])}`
        : "";
    const pollenRow = pollen.length
      ? `<div class="aq-pollen">
          <span class="aq-pollen-head">Pollen</span>
          ${pollen
            .map(
              (p) =>
                `<span class="aq-pollen-item">${escapeHtml(p.label)} <strong>${Math.round(p.value)}</strong></span>`,
            )
            .join("")}
        </div>`
      : "";
    el.innerHTML = `<div class="airquality-stage" data-band="${BANDS[bandIndex(value)].key}">
      <div class="aq-now">
        <span class="aq-now-icon">${AQI_ICON}</span>
        <div class="aq-now-main">
          <div class="aq-now-value">${Math.round(value)}<span class="aq-now-unit">AQI</span></div>
          <div class="aq-now-category">${escapeHtml(aqi.category)}</div>
        </div>
        <div class="aq-now-side">
          <div class="aq-now-loc">${escapeHtml(data.location ?? "")}</div>
          <div class="aq-now-meta">${aqi.dominant ? `Dominant: ${escapeHtml(aqi.dominant)}` : ""}</div>
        </div>
      </div>
      ${bandGauge(value)}
      ${
        pollutants.length
          ? `<div class="aq-pollutants">${pollutants
              .map((p, i) => pollutantCard(p, maxSub, i === 0))
              .join("")}</div>`
          : ""
      }
      ${
        hours.length >= 2
          ? `<div class="aq-hourly">
              <div class="aq-hourly-head">
                <span>AQI · next ${hours.length} hours</span>
                <span class="aq-hourly-trend">${trend}</span>
              </div>
              <div class="aq-graph-box">${trendGraph(hours)}</div>
              <div class="aq-hour-labels">${hourTicks(hours)}</div>
            </div>`
          : ""
      }
      ${pollenRow}
    </div>`;
  },
});
