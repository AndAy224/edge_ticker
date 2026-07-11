// Upcoming rocket launches (Launch Library 2, detailed): featured launch with
// booster/landing/streak/orbit/GO-probability detail and a live countdown,
// plus a short list and a recent-results strip. Cape Canaveral / Kennedy
// launches are highlighted — visible from the Tampa Bay area, spectacularly
// so at night (real night check via the weather module's sunrise/sunset).
//
// Inside the live window (T−45m..T+15m) the whole pane becomes a launch-day
// countdown scene; the ticking interval detects window transitions between
// payloads and redraws itself.
//
// The countdown is the one sanctioned JS-timer exception among renderers:
// modules have no teardown hook, so the 1s interval clears itself as soon as
// its element leaves the DOM (the stage layer is removed ~320ms after the
// module rotates away or re-renders).
import { register } from "./registry";

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!,
  );
}

const ROCKET_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
  <path d="M12 2c3 2.5 4.5 6 4.5 9.5L14 14h-4l-2.5-2.5C7.5 8 9 4.5 12 2z"/>
  <circle cx="12" cy="8.5" r="1.6"/>
  <path d="M9.5 11.5 6 15l3 .5M14.5 11.5 18 15l-3 .5M10 14l-1 5 3-2 3 2-1-5"/>
</svg>`;

const LIVE_BEFORE_MS = 45 * 60 * 1000;
const LIVE_AFTER_MS = 15 * 60 * 1000;
const FINISHED = ["Success", "Failure", "Partial Failure"];

// Sunrise/sunset from the weather module, pushed in by main.ts (same pattern
// as setWeatherAlerts in modules/weather.ts).
let sun: { sunrise?: string; sunset?: string } | null = null;
export function setLaunchSun(next: any): void {
  sun = next ?? null;
}

/** Rough night check by local time-of-day (sun times are today's, launches
 *  may be tomorrow's — time-of-day is close enough for a viewing hint). */
function isNightLaunch(netIso: string): boolean {
  if (!sun?.sunrise || !sun?.sunset) return false;
  const minutes = (d: Date) => d.getHours() * 60 + d.getMinutes();
  const net = minutes(new Date(netIso));
  const rise = minutes(new Date(sun.sunrise));
  const set = minutes(new Date(sun.sunset));
  return net > set + 30 || net < rise;
}

function pad2(n: number): string {
  return String(Math.floor(Math.abs(n))).padStart(2, "0");
}

function countdown(netIso: string): string {
  const diff = (new Date(netIso).getTime() - Date.now()) / 1000;
  const sign = diff < 0 ? "T+" : "T−";
  const total = Math.abs(diff);
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = Math.floor(total % 60);
  return days > 0
    ? `${sign}${days}d ${pad2(hours)}:${pad2(minutes)}:${pad2(seconds)}`
    : `${sign}${pad2(hours)}:${pad2(minutes)}:${pad2(seconds)}`;
}

function netLabel(netIso: string): string {
  const net = new Date(netIso);
  const days = (net.getTime() - Date.now()) / 86400000;
  const opts: Intl.DateTimeFormatOptions =
    days < 6
      ? { weekday: "short", hour: "numeric", minute: "2-digit" }
      : { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" };
  return net.toLocaleString([], opts);
}

function inLiveWindow(l: any): boolean {
  if (!l?.net || FINISHED.includes(l.status)) return false;
  const delta = new Date(l.net).getTime() - Date.now();
  return delta <= LIVE_BEFORE_MS && delta >= -LIVE_AFTER_MS;
}

function isToday(netIso: string): boolean {
  const net = new Date(netIso);
  const now = new Date();
  return (
    net.getFullYear() === now.getFullYear() &&
    net.getMonth() === now.getMonth() &&
    net.getDate() === now.getDate()
  );
}

/** Starship flight day: overrides the featured slot until the flight happens
 *  (Success/Failure drops it) or scrubs (net moves off today's date). */
function isStarshipDay(l: any): boolean {
  return l.starship === true && isToday(l.net);
}

/** Featured = Starship flying today, else first Florida launch inside 48h,
 *  else the soonest upcoming. */
function pickFeatured(launches: any[]): any | undefined {
  const upcoming = launches.filter((l) => l.net && !FINISHED.includes(l.status));
  const starship = upcoming.find(isStarshipDay);
  const florida = upcoming.find(
    (l) => l.florida && new Date(l.net).getTime() - Date.now() < 48 * 3600 * 1000,
  );
  return starship ?? florida ?? upcoming[0];
}

function flightDayBadge(l: any): string {
  return isStarshipDay(l) ? `<div class="launch-flightday">STARSHIP FLIGHT DAY</div>` : "";
}

function statusPill(l: any): string {
  return `<span class="launch-status" data-status="${escapeHtml(l.status ?? "")}">${escapeHtml(
    l.status_text ?? l.status ?? "",
  )}</span>`;
}

function goPill(l: any): string {
  if (l.probability == null) return "";
  const band = l.probability >= 80 ? "go" : l.probability >= 50 ? "iffy" : "nogo";
  return `<span class="launch-go" data-band="${band}">GO ${Math.round(l.probability)}%</span>`;
}

function chipsRow(l: any): string {
  const programs = (l.programs ?? [])
    .slice(0, 2)
    .map((p: string) => `<span class="launch-chip program">${escapeHtml(p)}</span>`)
    .join("");
  const orbit = l.orbit
    ? `<span class="launch-chip orbit" title="${escapeHtml(l.orbit_name ?? "")}">${escapeHtml(l.orbit)}</span>`
    : "";
  return programs + orbit + goPill(l);
}

function boosterLine(l: any): string {
  const b = (l.boosters ?? [])[0];
  if (!b?.serial) return "";
  const flight = b.flight_no ? ` · flight ${b.flight_no}` : "";
  const landing = b.landing_attempt
    ? ` · lands ${escapeHtml(b.landing_type ?? "")}${
        b.landing_location ? ` “${escapeHtml(b.landing_location)}”` : ""
      }`
    : " · expended";
  return `<div class="launch-booster">${escapeHtml(b.serial)}${flight}${landing}</div>`;
}

function streakLine(l: any): string {
  const r = l.rocket ?? {};
  if (!r.full_name || r.total == null) return "";
  const streak = r.streak ? ` · ${r.streak} straight successes` : "";
  return `<div class="launch-streak">${escapeHtml(r.full_name)} · ${r.total} flights${streak}</div>`;
}

function whereLine(l: any): string {
  const padNo = l.pad_count != null ? ` · pad launch #${l.pad_count + 1}` : "";
  return `<div class="launch-where">${escapeHtml(l.pad ?? "")} · ${escapeHtml(
    l.location ?? "",
  )}${padNo}</div>`;
}

function visibilityHint(l: any): string {
  if (!l.florida) return "";
  return isNightLaunch(l.net)
    ? `<div class="launch-visible">Night launch — visible from the coast, look east</div>`
    : `<div class="launch-visible">Canaveral launch — look east ~2 min after liftoff</div>`;
}

function backdrop(l: any): string {
  return l.image
    ? `<img class="launch-backdrop" src="${escapeHtml(l.image)}" alt="" onerror="this.remove()">`
    : "";
}

function listRow(l: any): string {
  return `<div class="launch-row${l.florida ? " florida" : ""}">
    <span class="launch-row-net">${escapeHtml(netLabel(l.net))}</span>
    <span class="launch-row-name">${escapeHtml(l.name)}</span>
    <span class="launch-row-loc">${escapeHtml((l.location ?? "").split(",")[0])}</span>
  </div>`;
}

function recentStrip(recent: any[]): string {
  if (!recent?.length) return "";
  const items = recent
    .map((r) => {
      const ok = r.status === "Success";
      return `<span class="launch-recent-item ${ok ? "ok" : "bad"}">
        <span class="launch-recent-dot"></span>${escapeHtml((r.name ?? "").split(" | ").pop())}</span>`;
    })
    .join("");
  return `<div class="launch-recent"><span class="launch-recent-label">RECENT</span>${items}</div>`;
}

function idleScene(featured: any, rest: any[], recent: any[]): string {
  const [rocket, mission = ""] = String(featured.name ?? "").split(" | ");
  return `<div class="launch-stage">
    <div class="launch-featured${featured.florida ? " florida" : ""}${
      isStarshipDay(featured) ? " starship" : ""
    }">
      ${backdrop(featured)}
      <div class="launch-icon">${ROCKET_ICON}</div>
      <div class="launch-main">
        ${flightDayBadge(featured)}
        <div class="launch-provider">${escapeHtml(featured.provider ?? "")}
          ${statusPill(featured)} ${chipsRow(featured)}
        </div>
        <div class="launch-name">${escapeHtml(rocket)}</div>
        ${mission ? `<div class="launch-mission">${escapeHtml(mission)}</div>` : ""}
        ${
          featured.mission_description
            ? `<div class="launch-desc">${escapeHtml(featured.mission_description)}</div>`
            : ""
        }
        ${whereLine(featured)}
        ${boosterLine(featured)}
        ${streakLine(featured)}
        ${visibilityHint(featured)}
      </div>
      <div class="launch-count">
        <div class="launch-count-clock">${countdown(featured.net)}</div>
        <div class="launch-count-net">${escapeHtml(netLabel(featured.net))}</div>
      </div>
    </div>
    <div class="launch-list">${rest.map(listRow).join("")}</div>
    ${recentStrip(recent)}
  </div>`;
}

function liveScene(featured: any): string {
  const [rocket, mission = ""] = String(featured.name ?? "").split(" | ");
  return `<div class="launch-stage">
    <div class="launch-live${featured.florida ? " florida" : ""}${
      isStarshipDay(featured) ? " starship" : ""
    }">
      ${backdrop(featured)}
      ${flightDayBadge(featured)}
      <div class="launch-live-head">
        <span class="launch-provider">${escapeHtml(featured.provider ?? "")}</span>
        ${statusPill(featured)} ${goPill(featured)}
      </div>
      <div class="launch-count-clock">${countdown(featured.net)}</div>
      <div class="launch-live-name">${escapeHtml(rocket)}${
        mission ? ` — ${escapeHtml(mission)}` : ""
      }</div>
      ${whereLine(featured)}
      ${boosterLine(featured)}
      ${visibilityHint(featured)}
      ${
        featured.weather_concerns
          ? `<div class="launch-weather">${escapeHtml(featured.weather_concerns)}</div>`
          : ""
      }
    </div>
  </div>`;
}

function draw(el: HTMLElement, data: any): void {
  const launches: any[] = (data?.launches ?? []).filter((l: any) => l?.net);
  const featured = pickFeatured(launches);
  if (!featured) {
    el.innerHTML = `<div class="empty">No upcoming launches</div>`;
    return;
  }
  const liveMode = inLiveWindow(featured);
  el.innerHTML = liveMode
    ? liveScene(featured)
    : idleScene(
        featured,
        launches.filter((l) => l !== featured).slice(0, 4),
        data?.recent ?? [],
      );

  const clock = el.querySelector<HTMLElement>(".launch-count-clock")!;
  const timer = window.setInterval(() => {
    if (!el.isConnected) {
      clearInterval(timer);
      return;
    }
    // Crossing T−45m / T+15m between payloads: swap scenes ourselves.
    if (inLiveWindow(featured) !== liveMode) {
      clearInterval(timer);
      draw(el, data);
      return;
    }
    clock.textContent = countdown(featured.net);
  }, 1000);
}

register({
  id: "launches",
  renderStage(el, data) {
    draw(el, data);
  },
});
