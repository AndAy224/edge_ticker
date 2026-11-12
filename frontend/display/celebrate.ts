// Full-screen score celebration: the sport's ball flies across the panel with
// firework bursts (Phase A, ~2.2s), then a card with the scorer's photo, the
// play, and the score (Phase B, ~10s). Tap dismisses; events queue while one
// is playing, and while a takeover that would cover them is up (`isHeld`).
// All animation is transform/opacity (GPU compositor path).

import { sportIcon } from "./icons";

const FLIGHT_MS = 2300;
const CARD_MS = 10_000;
const GAP_MS = 500; // between one celebration and the next
const MAX_QUEUE = 3;
// The longest the queue alone can make an event wait (the third in line), plus
// one slot of slack so timer drift never costs that third its turn. One that
// waited longer — behind a camera takeover, say — is old news: dropped.
const STALE_MS = (MAX_QUEUE + 1) * (FLIGHT_MS + CARD_MS + GAP_MS);
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
  private queue: { event: any; at: number }[] = [];
  private active = false;
  private gap = false; // between two queued celebrations; its timer is in `timers`
  private timers: number[] = [];

  constructor(
    private el: HTMLElement,
    private isBlanked: () => boolean,
    // Something that draws over this overlay is up: hold events, don't play
    // them where nobody can see. The owner calls resume() once it may be gone.
    private isHeld: () => boolean = () => false,
  ) {
    el.addEventListener("pointerdown", () => this.dismiss());
  }

  isOpen(): boolean {
    return this.active;
  }

  show(event: any): void {
    if (!event || this.isBlanked()) return;
    this.preload(event); // warm image cache during the flight phase
    if (this.active || this.isHeld()) {
      if (this.queue.length < MAX_QUEUE) this.queue.push({ event, at: Date.now() });
      return;
    }
    this.play(event);
  }

  /** Play the next queued event, if nothing is playing or covering us: after
   *  a celebration ends, and (the owner's call) after a hold is over. Events
   *  stay queued until they actually start, so a hold keeps their order. */
  resume(): void {
    if (this.active || this.gap || !this.queue.length || this.isHeld()) return;
    if (this.isBlanked()) {
      this.queue = []; // same as show(): nobody is watching
      return;
    }
    const cutoff = Date.now() - STALE_MS;
    while (this.queue.length) {
      const next = this.queue.shift()!;
      if (next.at >= cutoff) {
        this.play(next.event);
        return;
      }
    }
  }

  private preload(event: any): void {
    for (const url of [
      event.scorer?.headshot,
      event.team?.logo,
      event.opponent?.logo,
    ]) {
      if (url) new Image().src = url;
    }
  }

  private later(fn: () => void, ms: number): void {
    this.timers.push(window.setTimeout(fn, ms));
  }

  private scene(): HTMLElement | null {
    return this.el.querySelector<HTMLElement>(".celebrate-scene");
  }

  private play(event: any): void {
    this.active = true;
    const color = event.team?.color ? `#${event.team.color}` : "#4da3ff";
    const sport = String(event.sport ?? "").replace(/[^a-z]/g, "") || "generic";
    this.el.style.setProperty("--celebrate-color", color);
    this.el.classList.remove("hidden");
    this.el.classList.add(`sport-${sport}`);
    // The full-screen team-colored field is the stage: ball + fireworks play
    // on top of it, then the whole scene crossfades into the card.
    this.el.innerHTML = `<div class="celebrate-scene">
      <div class="celebrate-field"></div>
      ${event.team?.logo ? `<img class="celebrate-field-logo" src="${esc(event.team.logo)}" alt="">` : ""}
      <div class="celebrate-ball">${sportIcon(event.sport)}</div>
    </div>`;
    for (const b of BURSTS) this.later(() => this.burst(b.x, b.y, color), b.at);
    this.later(() => this.showCard(event), FLIGHT_MS);
  }

  private burst(xPct: number, yPct: number, color: string): void {
    const scene = this.scene();
    if (!this.active || !scene) return;
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
      scene.appendChild(p);
      requestAnimationFrame(() =>
        requestAnimationFrame(() => p.classList.add("fly")),
      );
      this.later(() => p.remove(), 1500);
    }
  }

  private showCard(event: any): void {
    if (!this.active) return;
    // Crossfade: flight scene out, card in (over the dark backdrop).
    const scene = this.scene();
    if (scene) {
      scene.classList.add("fade-out");
      this.later(() => scene.remove(), 600);
    }
    const away = event.team_is_home ? event.opponent : event.team;
    const home = event.team_is_home ? event.team : event.opponent;
    const photo = event.scorer?.headshot ?? event.team?.logo ?? "";
    const card = document.createElement("div");
    card.className = "celebrate-card";
    card.innerHTML = `
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
      </div>`;
    this.el.appendChild(card);
    const img = card.querySelector<HTMLImageElement>(".celebrate-photo");
    if (img && event.team?.logo && img.src !== event.team.logo) {
      img.addEventListener("error", () => (img.src = event.team.logo), { once: true });
    }
    this.later(() => this.dismiss(), CARD_MS);
  }

  dismiss(): void {
    if (!this.active) return;
    for (const t of this.timers) clearTimeout(t);
    this.timers = [];
    this.active = false;
    this.el.className = "hidden"; // also drops the sport-* class
    this.el.innerHTML = "";
    // Cleared with the other timers above if a celebration that started
    // inside the gap is itself dismissed; this then sets it afresh.
    this.gap = this.queue.length > 0;
    if (this.gap) {
      this.later(() => {
        this.gap = false;
        this.resume();
      }, GAP_MS);
    }
  }
}
