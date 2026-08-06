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

/** Word labels for the common speed counts; anything else falls back to "N%". */
const SPEED_LABELS: Record<number, string[]> = {
  1: ["On"],
  2: ["Low", "High"],
  3: ["Low", "Med", "High"],
  4: ["Low", "Med", "High", "Max"],
};

interface FanSpeedButton {
  label: string;
  /** the data-* attribute the click handler dispatches on */
  attr: string;
  active: boolean;
}

/**
 * One button per stop the fan actually has: Off plus each speed. Percentage fans
 * get their stops from `percentage_step` (a 3-speed fan reports 33.33 → Off/Low/
 * Med/High); preset-only fans get one button per named mode. A fan with neither
 * is plain on/off and gets no row at all — its name button already toggles it.
 */
function fanSpeeds(attrs: Record<string, any> | undefined, on: boolean): FanSpeedButton[] {
  if (!attrs) return [];
  const off = (active: boolean): FanSpeedButton => ({
    label: "Off",
    attr: 'data-fan-off="1"',
    active,
  });

  if (typeof attrs.percentage === "number" || typeof attrs.percentage_step === "number") {
    const step =
      typeof attrs.percentage_step === "number" && attrs.percentage_step > 0
        ? attrs.percentage_step
        : 100 / 3;
    const count = Math.min(Math.max(Math.round(100 / step), 1), 6);
    // Home Assistant buckets a percentage back into a named speed by integer
    // division — speed i covers everything up to `(i * 100) // count` — so speed
    // i *is* that bound: a 3-speed fan is 33/66/100, not 33/67/100. Rounding
    // i * step instead put Med at 67, one over the bound, and the fan quietly
    // ran High. The same bound also un-breaks continuous fans (step 1), where
    // i * step asked for 1%..6%.
    const bound = (i: number) => Math.floor((i * 100) / count);
    const pct = typeof attrs.percentage === "number" ? attrs.percentage : 0;
    // A fan reported off keeps no active speed even if it remembers a percentage.
    let current = 0;
    if (on && pct > 0) {
      current = count;
      for (let i = 1; i <= count; i++) {
        if (pct <= bound(i)) {
          current = i;
          break;
        }
      }
    }
    const labels = SPEED_LABELS[count];
    const buttons = [off(current === 0)];
    for (let i = 1; i <= count; i++) {
      buttons.push({
        label: labels ? labels[i - 1]! : `${bound(i)}%`,
        attr: `data-fan-pct="${bound(i)}"`,
        active: current === i,
      });
    }
    return buttons;
  }

  const modes: string[] = Array.isArray(attrs.preset_modes) ? attrs.preset_modes : [];
  if (!modes.length) return [];
  return [
    off(!on),
    ...modes.slice(0, 5).map((mode) => ({
      label: mode,
      attr: `data-fan-preset="${escapeHtml(mode)}"`,
      active: on && attrs.preset_mode === mode,
    })),
  ];
}

export class HAOverlay {
  private mapping: HAMapping = { scenes: [], lights: [], fans: [], climate: null, media: null };
  private states: Record<string, HAEntityState> = {};
  private status = "unconfigured";
  private ip: string | null = null;
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
      fans: mapping?.fans ?? [],
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

  /** The appliance's LAN address, from the snapshot. Shown regardless of HA status. */
  setSystem(ip: string | null): void {
    this.ip = ip;
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
    // Absolutely positioned in the overlay's padding gutter, so it adds no
    // layout of its own and the columns render identically in every theme.
    const ipChip = this.ip ? `<div class="ha-ip">${escapeHtml(this.ip)}</div>` : "";
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

    // Fan tiles: name toggles on/off (generic data-domain path); one button per
    // speed stop, so a speed change is a single absolute service call rather
    // than a run of relative steps. Handled in onClick().
    const fans = this.mapping.fans
      .slice(0, 8)
      .map((id) => {
        const s = this.states[id];
        const on = s?.state === "on";
        const pct = s?.attributes?.percentage;
        const preset = s?.attributes?.preset_mode;
        // "On · 66%" while running; a fan that is off just reads "Off".
        const speed = typeof pct === "number" ? `${pct}%` : preset ? escapeHtml(preset) : "";
        const state = on ? (speed ? `On · ${speed}` : "On") : "Off";
        const buttons = fanSpeeds(s?.attributes, on)
          .map(
            (b) => `<button class="fan-speed-btn ${b.active ? "active" : ""}"
              ${offline ? "disabled" : ""} data-entity="${escapeHtml(id)}"
              ${b.attr}>${escapeHtml(b.label)}</button>`,
          )
          .join("");
        return `<div class="ha-tile fan ${on ? "on" : ""}">
          <button class="fan-toggle" ${offline ? "disabled" : ""}
            data-domain="fan" data-service="toggle" data-entity="${escapeHtml(id)}">
            <span class="tile-name">${escapeHtml(this.friendlyName(id))}</span>
            <span class="tile-state">${state}</span>
          </button>
          ${buttons ? `<span class="fan-speeds">${buttons}</span>` : ""}
        </div>`;
      })
      .join("");

    // .ha-col-title / .ha-hint are display:none by default; the glance theme
    // (and any future theme) reveals them via CSS.
    this.el.innerHTML = `${ipChip}${banner}
      <div class="ha-columns">
        <div class="ha-col scenes"><div class="ha-col-title">Scenes</div>${scenes || '<div class="ha-empty">No scenes mapped</div>'}</div>
        <div class="ha-col-wrap"><div class="ha-col-title">Lights</div><div class="ha-col lights-grid">${lights || '<div class="ha-empty">No lights mapped</div>'}</div></div>
        <div class="ha-col side"><div class="ha-col-title">Controls</div>${climate}${fans}${media}</div>
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

    // Fan speed row: absolute service calls, so one tap is one call and tapping
    // the speed you are already on is a no-op at the fan.
    const fanBtn = target.closest<HTMLElement>(
      "[data-fan-off], [data-fan-pct], [data-fan-preset]",
    );
    if (fanBtn) {
      const entityId = fanBtn.dataset.entity!;
      if (fanBtn.dataset.fanOff) {
        this.send("fan", "turn_off", entityId);
      } else if (fanBtn.dataset.fanPct) {
        this.send("fan", "set_percentage", entityId, {
          percentage: Number(fanBtn.dataset.fanPct),
        });
      } else {
        this.send("fan", "set_preset_mode", entityId, {
          preset_mode: fanBtn.dataset.fanPreset,
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
