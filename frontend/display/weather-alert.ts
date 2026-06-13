// Full-screen severe-weather takeover: single-phase card (no flight scene) on
// an amber-on-deep-red backdrop so it's unmistakable next to a team-colored
// score celebration. Tap dismisses; alerts queue while one is showing.
// All animation is transform/opacity (GPU compositor path).

import { WEATHER_ICONS } from "./icons";

const CARD_MS = 25_000;
const MAX_QUEUE = 2;

function esc(value: unknown): string {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!,
  );
}

function untilTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

export class WeatherAlertOverlay {
  private queue: any[] = [];
  private active = false;
  private timer = 0;

  constructor(
    private el: HTMLElement,
    private isBlanked: () => boolean,
    private wakeDisplay: () => void,
  ) {
    el.addEventListener("pointerdown", () => this.dismiss());
  }

  show(alert: any): void {
    if (!alert) return;
    if (this.isBlanked()) {
      // A manually-blanked panel is a deliberate act — but an Extreme warning
      // (tornado class) is life-safety, so it wakes the display. Severe
      // (routine thunderstorm warnings) stays suppressed.
      if (alert.severity !== "Extreme") return;
      this.wakeDisplay();
    }
    if (this.active) {
      if (this.queue.length < MAX_QUEUE) this.queue.push(alert);
      return;
    }
    this.play(alert);
  }

  private play(alert: any): void {
    this.active = true;
    const until = untilTime(alert.ends);
    this.el.classList.remove("hidden");
    this.el.innerHTML = `<div class="wxalert-card">
      <div class="wxalert-icon">${WEATHER_ICONS.warning}</div>
      <div class="wxalert-info">
        <div class="wxalert-event">${esc(alert.event)}</div>
        ${alert.headline ? `<div class="wxalert-headline">${esc(alert.headline)}</div>` : ""}
        <div class="wxalert-meta">
          ${alert.area ? `<span>${esc(alert.area)}</span>` : ""}
          ${until ? `<span>Until ${esc(until)}</span>` : ""}
        </div>
        ${alert.instruction ? `<div class="wxalert-instruction">${esc(alert.instruction)}</div>` : ""}
      </div>
    </div>`;
    this.timer = window.setTimeout(() => this.dismiss(), CARD_MS);
  }

  private dismiss(): void {
    if (!this.active) return;
    clearTimeout(this.timer);
    this.active = false;
    this.el.classList.add("hidden");
    this.el.innerHTML = "";
    const next = this.queue.shift();
    if (next) this.timer = window.setTimeout(() => this.show(next), 500);
  }
}
