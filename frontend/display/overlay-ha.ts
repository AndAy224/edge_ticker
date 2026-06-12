// Home automation surface: scenes column / lights grid / climate+media column.
// Tile taps send service calls over /ws/display; tiles re-render from live
// state pushes, so there is no optimistic state to get wrong.

import type { HAEntityState, HAMapping } from "./types";

export type ActionSender = (
  domain: string,
  service: string,
  entityId?: string,
  data?: Record<string, unknown>,
) => void;

const IDLE_DISMISS_MS = 30_000;

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!,
  );
}

export class HAOverlay {
  private mapping: HAMapping = { scenes: [], lights: [], climate: null, media: null };
  private states: Record<string, HAEntityState> = {};
  private status = "unconfigured";
  private idleTimer = 0;

  constructor(
    private el: HTMLElement,
    private send: ActionSender,
  ) {
    el.addEventListener("click", (e) => this.onClick(e));
    el.addEventListener("pointerdown", () => this.resetIdle());
  }

  isOpen(): boolean {
    return !this.el.classList.contains("hidden");
  }

  open(): void {
    this.render();
    this.el.classList.remove("hidden");
    this.resetIdle();
  }

  close(): void {
    this.el.classList.add("hidden");
    clearTimeout(this.idleTimer);
  }

  setMapping(mapping: Partial<HAMapping> | undefined): void {
    this.mapping = {
      scenes: mapping?.scenes ?? [],
      lights: mapping?.lights ?? [],
      climate: mapping?.climate ?? null,
      media: mapping?.media ?? null,
    };
    if (this.isOpen()) this.render();
  }

  setStates(states: Record<string, HAEntityState>, status?: string): void {
    this.states = states ?? {};
    if (status) this.status = status;
    if (this.isOpen()) this.render();
  }

  updateState(entityId: string, state: HAEntityState): void {
    this.states[entityId] = state;
    if (this.isOpen()) this.render();
  }

  setStatus(status: string): void {
    this.status = status;
    if (this.isOpen()) this.render();
  }

  private resetIdle(): void {
    clearTimeout(this.idleTimer);
    this.idleTimer = window.setTimeout(() => this.close(), IDLE_DISMISS_MS);
  }

  private friendlyName(entityId: string): string {
    return (
      this.states[entityId]?.attributes?.friendly_name ??
      entityId.split(".")[1]?.replace(/_/g, " ") ??
      entityId
    );
  }

  private render(): void {
    const offline = this.status !== "connected";
    const banner = offline
      ? `<div class="ha-banner">${
          this.status === "unconfigured"
            ? "Home Assistant not configured"
            : "Home Assistant unreachable"
        }</div>`
      : "";

    const scenes = this.mapping.scenes
      .slice(0, 4)
      .map(
        (id) => `<button class="ha-tile scene" ${offline ? "disabled" : ""}
          data-domain="scene" data-service="turn_on" data-entity="${escapeHtml(id)}">
          ${escapeHtml(this.friendlyName(id))}
        </button>`,
      )
      .join("");

    const lights = this.mapping.lights
      .slice(0, 8)
      .map((id) => {
        const on = this.states[id]?.state === "on";
        return `<button class="ha-tile light ${on ? "on" : ""}" ${offline ? "disabled" : ""}
          data-domain="light" data-service="toggle" data-entity="${escapeHtml(id)}">
          <span class="tile-name">${escapeHtml(this.friendlyName(id))}</span>
          <span class="tile-state">${on ? "On" : "Off"}</span>
        </button>`;
      })
      .join("");

    let climate = "";
    if (this.mapping.climate) {
      const id = this.mapping.climate;
      const s = this.states[id];
      const current = s?.attributes?.current_temperature;
      const target = s?.attributes?.temperature;
      climate = `<div class="ha-tile climate">
        <span class="tile-name">${escapeHtml(this.friendlyName(id))}</span>
        <span class="climate-current">${current != null ? `${current}°` : "—"}</span>
        <span class="climate-controls">
          <button class="climate-btn" ${offline ? "disabled" : ""} data-climate-delta="-1" data-entity="${escapeHtml(id)}">−</button>
          <span class="climate-target">${target != null ? `${target}°` : "—"}</span>
          <button class="climate-btn" ${offline ? "disabled" : ""} data-climate-delta="1" data-entity="${escapeHtml(id)}">+</button>
        </span>
      </div>`;
    }

    let media = "";
    if (this.mapping.media) {
      const id = this.mapping.media;
      const s = this.states[id];
      const title = s?.attributes?.media_title;
      media = `<div class="ha-tile media">
        <span class="tile-name">${escapeHtml(this.friendlyName(id))}</span>
        <span class="tile-state">${escapeHtml(title ?? s?.state ?? "—")}</span>
        <button class="media-btn" ${offline ? "disabled" : ""}
          data-domain="media_player" data-service="media_play_pause" data-entity="${escapeHtml(id)}">⏯</button>
      </div>`;
    }

    // .ha-col-title / .ha-hint are display:none by default; the glance theme
    // (and any future theme) reveals them via CSS.
    this.el.innerHTML = `${banner}
      <div class="ha-columns">
        <div class="ha-col scenes"><div class="ha-col-title">Scenes</div>${scenes || '<div class="ha-empty">No scenes mapped</div>'}</div>
        <div class="ha-col-wrap"><div class="ha-col-title">Lights</div><div class="ha-col lights-grid">${lights || '<div class="ha-empty">No lights mapped</div>'}</div></div>
        <div class="ha-col side"><div class="ha-col-title">Climate &amp; Media</div>${climate}${media}</div>
      </div>
      <div class="ha-hint">swipe down to close</div>`;
  }

  private onClick(e: Event): void {
    if (this.status !== "connected") return;
    const target = e.target as HTMLElement;

    const climateBtn = target.closest<HTMLElement>("[data-climate-delta]");
    if (climateBtn) {
      const entityId = climateBtn.dataset.entity!;
      const current = this.states[entityId]?.attributes?.temperature;
      if (current != null) {
        this.send("climate", "set_temperature", entityId, {
          temperature: current + Number(climateBtn.dataset.climateDelta),
        });
      }
      return;
    }

    const actionEl = target.closest<HTMLElement>("[data-domain]");
    if (actionEl) {
      this.send(actionEl.dataset.domain!, actionEl.dataset.service!, actionEl.dataset.entity);
    }
  }
}
