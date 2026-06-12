import { WEATHER_ICONS, weatherIcon } from "../icons";
import { register } from "./registry";

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!,
  );
}

function weekday(date: string, index: number): string {
  if (index === 0) return "Today";
  const d = new Date(`${date}T12:00:00`);
  return d.toLocaleDateString([], { weekday: "short" });
}

function hourLabel(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "numeric" });
}

function hourTime(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

const GRAPH_W = 1000;
const GRAPH_H = 240;
const BAR_AREA = 70; // bottom band reserved for precip bars

/** Temperature curve with gradient fill + precip-probability bars. */
function hourlyGraph(hours: any[]): string {
  const temps = hours.map((h) => h.temp).filter((t: any) => t != null);
  if (temps.length < 2) return "";
  const min = Math.min(...temps);
  const max = Math.max(...temps);
  const span = max - min || 1;
  const x = (i: number) => (i / (hours.length - 1)) * GRAPH_W;
  const y = (t: number) => 44 + (1 - (t - min) / span) * (GRAPH_H - BAR_AREA - 56);
  const points = hours
    .map((h, i) => `${x(i).toFixed(1)},${y(h.temp ?? min).toFixed(1)}`)
    .join(" ");
  const bars = hours
    .map((h, i) => {
      const p = h.precip ?? 0;
      if (p <= 5) return "";
      const barH = (p / 100) * BAR_AREA;
      const barW = GRAPH_W / hours.length - 6;
      return `<rect x="${(x(i) - barW / 2).toFixed(1)}" y="${(GRAPH_H - barH).toFixed(1)}"
        width="${barW.toFixed(1)}" height="${barH.toFixed(1)}" rx="3" class="wx-precip-bar"/>`;
    })
    .join("");
  const minIdx = hours.findIndex((h) => h.temp === min);
  const maxIdx = hours.findIndex((h) => h.temp === max);
  const label = (i: number, t: number, above: boolean) =>
    `<text x="${Math.min(GRAPH_W - 40, Math.max(40, x(i))).toFixed(1)}"
       y="${(y(t) + (above ? -12 : 26)).toFixed(1)}" class="wx-temp-label">${Math.round(t)}°</text>`;
  return `<svg class="wx-graph" viewBox="0 0 ${GRAPH_W} ${GRAPH_H}" preserveAspectRatio="none">
    <defs><linearGradient id="wx-grad" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" class="wx-fill-top"/>
      <stop offset="100%" class="wx-fill-bottom"/>
    </linearGradient></defs>
    ${bars}
    <polygon fill="url(#wx-grad)" stroke="none"
      points="0,${GRAPH_H - BAR_AREA} ${points} ${GRAPH_W},${GRAPH_H - BAR_AREA}"/>
    <polyline class="wx-temp-line" points="${points}"/>
    ${label(maxIdx, max, true)}${minIdx !== maxIdx ? label(minIdx, min, true) : ""}
  </svg>`;
}

function dayCard(day: any, i: number): string {
  return `<div class="wx-day">
    <div class="wx-day-name">${weekday(day.date, i)}</div>
    <span class="wx-day-icon">${weatherIcon(day.code)}</span>
    <div class="wx-day-temps"><strong>${Math.round(day.high)}°</strong> <span>${Math.round(day.low)}°</span></div>
    <div class="wx-day-precip">${day.precip != null ? `${day.precip}% rain` : ""}</div>
  </div>`;
}

register({
  id: "weather",
  renderStage(el, data) {
    const current = data?.current;
    if (!current) {
      el.innerHTML = `<div class="empty">Waiting for weather data…</div>`;
      return;
    }
    const daily: any[] = data?.daily ?? [];
    const hours: any[] = data?.hourly ?? [];
    const sun = data?.sun ?? {};
    const sunLine =
      sun.sunrise && sun.sunset
        ? `<span class="wx-sun"><span class="wx-sun-icon">${WEATHER_ICONS.sun}</span>
           ${hourTime(sun.sunrise)} · ${hourTime(sun.sunset)}</span>`
        : "";
    const labels = hours
      .map((h, i) =>
        i % 3 === 0 ? `<span>${hourLabel(h.time)}</span>` : "",
      )
      .join("");
    el.innerHTML = `<div class="weather-stage">
      <div class="wx-now">
        <span class="wx-now-icon">${weatherIcon(current.code)}</span>
        <div>
          <div class="wx-now-temp">${Math.round(current.temp)}°</div>
          <div class="wx-now-text">${escapeHtml(current.text)}</div>
          <div class="wx-now-meta">
            ${current.feels_like != null ? `Feels ${Math.round(current.feels_like)}°` : ""}
            ${current.humidity != null ? ` · ${current.humidity}% rh` : ""}
            ${current.wind != null ? ` · ${Math.round(current.wind)} mph` : ""}
          </div>
          <div class="wx-now-loc">${escapeHtml(data.location ?? "")}</div>
        </div>
      </div>
      ${
        hours.length >= 2
          ? `<div class="wx-hourly">
              <div class="wx-hourly-head"><span>Next 24 hours</span>${sunLine}</div>
              ${hourlyGraph(hours)}
              <div class="wx-hour-labels">${labels}</div>
            </div>`
          : ""
      }
      <div class="wx-days">${daily.slice(0, 3).map(dayCard).join("")}</div>
    </div>`;
  },
});
