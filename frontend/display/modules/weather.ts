import { WEATHER_ICONS, weatherIcon } from "../icons";
import { register } from "./registry";

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!,
  );
}

// Active NWS alerts, pushed in by main.ts from the weather_alerts module
// (renderers only receive their own payload — same pattern as
// setSportsLiveMode in modules/sports.ts).
let activeAlerts: any[] = [];
export function setWeatherAlerts(alerts: any[]): void {
  activeAlerts = alerts ?? [];
}

function alertBanner(): string {
  if (!activeAlerts.length) return "";
  const a = activeAlerts[0];
  const ends = a.ends ? new Date(a.ends) : null;
  const until =
    ends && !Number.isNaN(ends.getTime())
      ? ` — until ${ends.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`
      : "";
  const more = activeAlerts.length > 1 ? ` (+${activeAlerts.length - 1} more)` : "";
  return `<div class="wx-alert-banner">${WEATHER_ICONS.warning}<span>${escapeHtml(
    a.event,
  )}${until}${more}</span></div>`;
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

/** Temperature through the next 24 hours. Min/max sit in HTML tags over the
 *  box, not SVG text — the svg stretches with preserveAspectRatio="none",
 *  which would squash glyphs. */
function tempGraph(hours: any[]): string {
  const temps = hours.map((h) => h.temp).filter((t: any) => t != null);
  if (temps.length < 2) return "";
  const min = Math.min(...temps);
  const max = Math.max(...temps);
  const span = max - min || 1;
  const x = (i: number) => (i / (hours.length - 1)) * GRAPH_W;
  const y = (t: number) => 30 + (1 - (t - min) / span) * (GRAPH_H - 70);
  const points = hours
    .map((h, i) => `${x(i).toFixed(1)},${y(h.temp ?? min).toFixed(1)}`)
    .join(" ");
  const minIdx = hours.findIndex((h) => h.temp === min);
  const maxIdx = hours.findIndex((h) => h.temp === max);
  const tag = (i: number, t: number) => {
    const lx = Math.min(96, Math.max(4, (x(i) / GRAPH_W) * 100));
    const ly = (y(t) / GRAPH_H) * 100;
    return `<span class="wx-temp-tag" style="left:${lx.toFixed(1)}%;top:${ly.toFixed(1)}%">${Math.round(t)}°</span>`;
  };
  return `<div class="wx-graph-box"><svg class="wx-graph" viewBox="0 0 ${GRAPH_W} ${GRAPH_H}" preserveAspectRatio="none">
    <defs><linearGradient id="wx-grad" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" class="wx-fill-top"/>
      <stop offset="100%" class="wx-fill-bottom"/>
    </linearGradient></defs>
    <polygon fill="url(#wx-grad)" stroke="none" points="0,${GRAPH_H} ${points} ${GRAPH_W},${GRAPH_H}"/>
    <polyline class="wx-temp-line" points="${points}"/>
  </svg>${tag(maxIdx, max)}${minIdx !== maxIdx ? tag(minIdx, min) : ""}</div>`;
}

/** Rain chance as a band under the curve — one cell per hour, inked by
 *  probability, so a wet evening is visible as a block rather than a row of
 *  stubs. */
function rainBand(hours: any[]): string {
  if (!hours.length) return "";
  const cells = hours
    .map((h) => {
      const p = Math.min(100, Math.max(0, h.precip ?? 0));
      // ramp from the track colour to the accent — plain opacity left a 13%
      // hour and a 40% hour looking the same on a dark panel
      return `<span class="wx-rain-cell" style="background:color-mix(in srgb, var(--accent) ${p}%, var(--muted))"></span>`;
    })
    .join("");
  return `<div class="wx-rain">${cells}</div>`;
}

/** Hour ticks at their true x%, not distributed by space-between. */
function hourTicks(hours: any[]): string {
  const last = hours.length - 1;
  if (last < 1) return "";
  const picked = hours.map((_, i) => i).filter((i) => i % 3 === 0);
  if (last - picked[picked.length - 1] >= 3) picked.push(last);
  return picked
    .map((i) => {
      const pct = Math.min(97, Math.max(3, (i / last) * 100));
      return `<span style="left:${pct.toFixed(1)}%">${hourLabel(hours[i].time)}</span>`;
    })
    .join("");
}

/** Today's verdict: what the day is doing and when the wet part lands. */
function verdict(daily: any[], hours: any[]): string {
  const today = daily[0];
  const parts: string[] = [];
  if (today?.text) parts.push(`${today.text} today`);
  if (today?.precip != null && today.precip >= 10) parts.push(`${today.precip}% chance`);
  const main = parts.join(" · ") || "Today";
  let sub = "";
  const withRain = hours.filter((h) => h.precip != null);
  if (withRain.length) {
    const wettest = withRain.reduce((a, b) => (b.precip > a.precip ? b : a));
    if (wettest.precip >= 30) sub = `wettest around ${hourLabel(wettest.time)}`;
    else if (wettest.precip >= 15) sub = `slight chance around ${hourLabel(wettest.time)}`;
    else sub = "little rain expected";
  }
  return `<div class="wx-verdict">
    <div class="wx-verdict-main">${escapeHtml(main)}</div>
    ${sub ? `<div class="wx-verdict-sub">${escapeHtml(sub)}</div>` : ""}
  </div>`;
}

/** Sunrise → sunset with where we are in it, and how much light is left.
 *  Recomputed on every render, and the pane re-renders each time the module
 *  comes back around, so the figure is current whenever it's on screen. */
function daylight(sun: any): string {
  if (!sun?.sunrise || !sun?.sunset) return "";
  const rise = new Date(sun.sunrise).getTime();
  const set = new Date(sun.sunset).getTime();
  const now = Date.now();
  if (!(set > rise)) return "";
  const pct = Math.min(100, Math.max(0, ((now - rise) / (set - rise)) * 100));
  const daytime = now >= rise && now <= set;
  let note = "";
  if (now < rise) {
    note = `sunrise in ${duration(rise - now)}`;
  } else if (daytime) {
    note = `${duration(set - now)} of light left`;
  } else {
    note = "after sunset";
  }
  return `<div class="wx-daylight">
    <span class="wx-sun-icon">${WEATHER_ICONS.sun}</span>
    <span class="wx-dl-time">${hourTime(sun.sunrise)}</span>
    <span class="wx-dl-track">
      <span class="wx-dl-fill" style="width:${pct.toFixed(1)}%"></span>
      ${daytime ? `<span class="wx-dl-marker" style="left:${pct.toFixed(1)}%"></span>` : ""}
    </span>
    <span class="wx-dl-time">${hourTime(sun.sunset)}</span>
    <span class="wx-dl-note">${escapeHtml(note)}</span>
  </div>`;
}

function duration(ms: number): string {
  const minutes = Math.max(0, Math.round(ms / 60000));
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return h ? `${h}h ${m}m` : `${m}m`;
}

function dayCard(day: any, i: number): string {
  return `<div class="wx-day">
    <span class="wx-day-icon">${weatherIcon(day.code)}</span>
    <div class="wx-day-text">
      <div class="wx-day-name">${weekday(day.date, i)}</div>
      <div class="wx-day-temps">
        <strong>${Math.round(day.high)}°</strong> <span>${Math.round(day.low)}°</span>${
          day.precip != null ? `<span class="wx-day-precip"> · ${day.precip}%</span>` : ""
        }
      </div>
    </div>
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
    el.innerHTML = `<div class="weather-stage">
      ${alertBanner()}
      <div class="wx-head">
        ${verdict(daily, hours)}
        <div class="wx-nowchip">
          <span class="wx-nowchip-icon">${weatherIcon(current.code)}</span>
          <span class="wx-nowchip-temp">${Math.round(current.temp)}°</span>
          <span class="wx-nowchip-text">${escapeHtml(current.text)}${
            current.feels_like != null ? ` · feels ${Math.round(current.feels_like)}°` : ""
          }</span>
        </div>
      </div>
      ${
        hours.length >= 2
          ? `<div class="wx-timeline">
              ${tempGraph(hours)}
              ${rainBand(hours)}
              <div class="wx-hour-labels">${hourTicks(hours)}</div>
            </div>`
          : ""
      }
      ${daylight(data?.sun)}
      <div class="wx-days">${daily.slice(0, 5).map(dayCard).join("")}</div>
    </div>`;
  },
});
