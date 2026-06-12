// Full-screen score celebration: the sport's ball flies across the panel with
// firework bursts (Phase A, ~2.2s), then a card with the scorer's photo, the
// play, and the score (Phase B, ~10s). Tap dismisses; events queue while one
// is playing. All animation is transform/opacity (GPU compositor path).

import { sportIcon } from "./icons";

const FLIGHT_MS = 2300;
const CARD_MS = 10_000;
const MAX_QUEUE = 3;
const BURSTS = [
  { x: 18, y: 42, at: 350 },
  { x: 50, y: 28, at: 850 },
  { x: 80, y: 46, at: 1350 },
];
const PARTICLES_PER_BURST = 26;

function esc(value: unknown): string {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!,
  );
}

export class Celebration {
  private queue: any[] = [];
  private active = false;
  private timers: number[] = [];

  constructor(
    private el: HTMLElement,
    private isBlanked: () => boolean,
  ) {
    el.addEventListener("pointerdown", () => this.dismiss());
  }

  isOpen(): boolean {
    return this.active;
  }

  show(event: any): void {
    if (!event || this.isBlanked()) return;
    if (this.active) {
      if (this.queue.length < MAX_QUEUE) this.queue.push(event);
      return;
    }
    this.play(event);
  }

  private later(fn: () => void, ms: number): void {
    this.timers.push(window.setTimeout(fn, ms));
  }

  private play(event: any): void {
    this.active = true;
    const color = event.team?.color ? `#${event.team.color}` : "#4da3ff";
    this.el.style.setProperty("--celebrate-color", color);
    this.el.classList.remove("hidden");
    this.el.innerHTML = `<div class="celebrate-ball">${sportIcon(event.sport)}</div>`;
    for (const b of BURSTS) this.later(() => this.burst(b.x, b.y, color), b.at);
    this.later(() => this.showCard(event), FLIGHT_MS);
  }

  private burst(xPct: number, yPct: number, color: string): void {
    if (!this.active) return;
    const palette = [color, "#ffffff", "#ffd24d"];
    for (let i = 0; i < PARTICLES_PER_BURST; i++) {
      const p = document.createElement("span");
      p.className = "celebrate-particle";
      const angle = Math.random() * Math.PI * 2;
      const dist = 130 + Math.random() * 380;
      p.style.left = `${xPct}%`;
      p.style.top = `${yPct}%`;
      p.style.background = palette[i % palette.length];
      p.style.setProperty("--dx", `${Math.cos(angle) * dist}px`);
      p.style.setProperty("--dy", `${Math.sin(angle) * dist - 90}px`);
      this.el.appendChild(p);
      requestAnimationFrame(() =>
        requestAnimationFrame(() => p.classList.add("fly")),
      );
      this.later(() => p.remove(), 1500);
    }
  }

  private showCard(event: any): void {
    if (!this.active) return;
    const away = event.team_is_home ? event.opponent : event.team;
    const home = event.team_is_home ? event.team : event.opponent;
    const photo = event.scorer?.headshot ?? event.team?.logo ?? "";
    this.el.innerHTML = `<div class="celebrate-card">
      ${photo ? `<img class="celebrate-photo" src="${esc(photo)}" alt="">` : ""}
      <div class="celebrate-info">
        <div class="celebrate-label">${esc(event.label ?? "SCORE")}!</div>
        ${event.scorer?.name ? `<div class="celebrate-scorer">${esc(event.scorer.name)}</div>` : ""}
        ${event.text ? `<div class="celebrate-text">${esc(event.text)}</div>` : ""}
        <div class="celebrate-score">
          ${away?.logo ? `<img src="${esc(away.logo)}" alt="">` : ""}
          <span>${esc(away?.abbrev)} ${esc(event.away_score)}</span>
          <span class="celebrate-dash">—</span>
          <span>${esc(event.home_score)} ${esc(home?.abbrev)}</span>
          ${home?.logo ? `<img src="${esc(home.logo)}" alt="">` : ""}
        </div>
      </div>
    </div>`;
    const img = this.el.querySelector<HTMLImageElement>(".celebrate-photo");
    if (img && event.team?.logo && img.src !== event.team.logo) {
      img.addEventListener("error", () => (img.src = event.team.logo), { once: true });
    }
    this.later(() => this.dismiss(), CARD_MS);
  }

  private dismiss(): void {
    if (!this.active) return;
    for (const t of this.timers) clearTimeout(t);
    this.timers = [];
    this.active = false;
    this.el.classList.add("hidden");
    this.el.innerHTML = "";
    const next = this.queue.shift();
    if (next) this.later(() => this.show(next), 500);
  }
}
