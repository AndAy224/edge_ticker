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

const GRAPH_W = 1000;
const GRAPH_H = 240;

/** US-AQI curve with a gradient fill, scaled to the visible window. */
function aqiGraph(hours: any[]): string {
  const values = hours.map((h) => h.aqi).filter((v: any) => v != null);
  if (values.length < 2) return "";
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const x = (i: number) => (i / (hours.length - 1)) * GRAPH_W;
  const y = (v: number) => 44 + (1 - (v - min) / span) * (GRAPH_H - 100);
  const points = hours
    .map((h, i) => `${x(i).toFixed(1)},${y(h.aqi ?? min).toFixed(1)}`)
    .join(" ");
  const minIdx = hours.findIndex((h) => h.aqi === min);
  const maxIdx = hours.findIndex((h) => h.aqi === max);
  const label = (i: number, v: number) =>
    `<text x="${Math.min(GRAPH_W - 40, Math.max(40, x(i))).toFixed(1)}"
       y="${(y(v) - 12).toFixed(1)}" class="aq-graph-label">${Math.round(v)}</text>`;
  return `<svg class="aq-graph" viewBox="0 0 ${GRAPH_W} ${GRAPH_H}" preserveAspectRatio="none">
    <defs><linearGradient id="aq-grad" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" class="aq-fill-top"/>
      <stop offset="100%" class="aq-fill-bottom"/>
    </linearGradient></defs>
    <polygon fill="url(#aq-grad)" stroke="none"
      points="0,${GRAPH_H} ${points} ${GRAPH_W},${GRAPH_H}"/>
    <polyline class="aq-graph-line" points="${points}"/>
    ${label(maxIdx, max)}${minIdx !== maxIdx ? label(minIdx, min) : ""}
  </svg>`;
}

function pollutantCard(p: any): string {
  return `<div class="aq-pollutant">
    <div class="aq-pollutant-label">${escapeHtml(p.label)}</div>
    <div class="aq-pollutant-value">${Math.round(p.value)}<span>${escapeHtml(p.unit)}</span></div>
  </div>`;
}

register({
  id: "airquality",
  renderStage(el, data) {
    const aqi = data?.aqi;
    if (!aqi || aqi.value == null) {
      el.innerHTML = `<div class="empty">Waiting for air-quality data…</div>`;
      return;
    }
    const pollutants: any[] = data?.pollutants ?? [];
    const pollen: any[] = data?.pollen ?? [];
    const hours: any[] = data?.hourly ?? [];
    const labels = hours
      .map((h, i) => (i % 3 === 0 ? `<span>${hourLabel(h.time)}</span>` : ""))
      .join("");
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
    el.innerHTML = `<div class="airquality-stage" data-accent="${escapeHtml(aqi.accent)}">
      <div class="aq-now">
        <span class="aq-now-icon">${AQI_ICON}</span>
        <div>
          <div class="aq-now-value">${Math.round(aqi.value)}<span class="aq-now-unit">AQI</span></div>
          <div class="aq-now-category">${escapeHtml(aqi.category)}</div>
          <div class="aq-now-meta">
            ${aqi.dominant ? `Dominant: ${escapeHtml(aqi.dominant)}` : ""}
          </div>
          <div class="aq-now-loc">${escapeHtml(data.location ?? "")}</div>
        </div>
      </div>
      ${
        hours.length >= 2
          ? `<div class="aq-hourly">
              <div class="aq-hourly-head"><span>AQI · next 24 hours</span></div>
              ${aqiGraph(hours)}
              <div class="aq-hour-labels">${labels}</div>
            </div>`
          : ""
      }
      ${pollutants.length ? `<div class="aq-pollutants">${pollutants.map(pollutantCard).join("")}</div>` : ""}
      ${pollenRow}
    </div>`;
  },
});
