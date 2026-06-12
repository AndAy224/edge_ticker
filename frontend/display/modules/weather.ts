import { weatherIcon } from "../icons";
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
      <div class="wx-days">${daily.slice(0, 3).map(dayCard).join("")}</div>
    </div>`;
  },
});
