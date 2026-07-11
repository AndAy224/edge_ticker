// Upcoming rocket launches (Launch Library 2): featured next launch with a
// live countdown plus a short list. Cape Canaveral / Kennedy launches are
// highlighted — visible from the Tampa Bay area on a clear night.
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

/** Featured = first Florida launch inside 48h, else the soonest upcoming. */
function pickFeatured(launches: any[]): any | undefined {
  const upcoming = launches.filter(
    (l) => l.net && l.status !== "Success" && l.status !== "Failure",
  );
  const florida = upcoming.find(
    (l) => l.florida && new Date(l.net).getTime() - Date.now() < 48 * 3600 * 1000,
  );
  return florida ?? upcoming[0];
}

function listRow(l: any): string {
  return `<div class="launch-row${l.florida ? " florida" : ""}">
    <span class="launch-row-net">${escapeHtml(netLabel(l.net))}</span>
    <span class="launch-row-name">${escapeHtml(l.name)}</span>
    <span class="launch-row-loc">${escapeHtml((l.location ?? "").split(",")[0])}</span>
  </div>`;
}

register({
  id: "launches",
  renderStage(el, data) {
    const launches: any[] = (data?.launches ?? []).filter((l: any) => l?.net);
    const featured = pickFeatured(launches);
    if (!featured) {
      el.innerHTML = `<div class="empty">No upcoming launches</div>`;
      return;
    }
    const rest = launches.filter((l) => l !== featured).slice(0, 4);
    const [rocket, mission = ""] = String(featured.name ?? "").split(" | ");

    el.innerHTML = `<div class="launch-stage">
      <div class="launch-featured${featured.florida ? " florida" : ""}">
        <div class="launch-icon">${ROCKET_ICON}</div>
        <div class="launch-main">
          <div class="launch-provider">${escapeHtml(featured.provider ?? "")}
            <span class="launch-status" data-status="${escapeHtml(featured.status ?? "")}">${escapeHtml(
              featured.status_text ?? featured.status ?? "",
            )}</span>
          </div>
          <div class="launch-name">${escapeHtml(rocket)}</div>
          ${mission ? `<div class="launch-mission">${escapeHtml(mission)}</div>` : ""}
          <div class="launch-where">${escapeHtml(featured.pad ?? "")} · ${escapeHtml(
            featured.location ?? "",
          )}</div>
          ${
            featured.florida
              ? `<div class="launch-visible">Canaveral launch — visible from the coast, look east</div>`
              : ""
          }
        </div>
        <div class="launch-count">
          <div class="launch-count-clock">${countdown(featured.net)}</div>
          <div class="launch-count-net">${escapeHtml(netLabel(featured.net))}</div>
        </div>
      </div>
      <div class="launch-list">${rest.map(listRow).join("")}</div>
    </div>`;

    const clock = el.querySelector<HTMLElement>(".launch-count-clock")!;
    const timer = window.setInterval(() => {
      if (!el.isConnected) {
        clearInterval(timer);
        return;
      }
      clock.textContent = countdown(featured.net);
    }, 1000);
  },
});
