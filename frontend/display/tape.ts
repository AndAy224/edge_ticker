// Marquee tape: two copies of the item run, translated -50% on a loop so the
// seam is invisible. Transform animation keeps it on the GPU compositor path.
//
// Live updates (streaming quotes arrive every ~2s) must not restart the
// animation or the tape visibly resets: when the item count is unchanged we
// mutate the existing spans in place and leave the animation running. Only a
// structural change (items added/removed) rebuilds the track. --tape-duration
// is likewise only set on rebuild — changing it mid-animation remaps the
// elapsed-time fraction onto the new timeline and the tape jumps.

import { sportIcon } from "./icons";
import type { TapeItem } from "./types";

const SPEED_PX_PER_SECOND = 120;
const MIN_DURATION_SECONDS = 10;

function fillItem(span: HTMLElement, item: TapeItem): void {
  span.className = `tape-item accent-${item.accent}`;
  if (item.icon) {
    const icon = document.createElement("span");
    icon.className = "tape-icon";
    icon.innerHTML = sportIcon(item.icon);
    span.replaceChildren(icon, document.createTextNode(item.text));
  } else {
    span.textContent = item.text;
  }
}

export class Tape {
  private signature = "";
  private itemSpans: HTMLElement[] = []; // spans across both halves, item i at [i] and [i + items.length]

  constructor(private track: HTMLElement) {}

  setItems(items: TapeItem[]): void {
    if (!items.length) return;
    const signature = items.map((i) => `${i.accent}|${i.icon ?? ""}|${i.text}`).join(" ");
    if (signature === this.signature) return;
    this.signature = signature;

    if (this.itemSpans.length === items.length * 2) {
      this.update(items);
    } else {
      this.rebuild(items);
    }
  }

  private update(items: TapeItem[]): void {
    items.forEach((item, i) => {
      for (const span of [this.itemSpans[i], this.itemSpans[i + items.length]]) {
        fillItem(span, item);
      }
    });
  }

  private rebuild(items: TapeItem[]): void {
    const half = document.createElement("div");
    half.className = "tape-half";
    for (const item of items) {
      const span = document.createElement("span");
      fillItem(span, item);
      half.appendChild(span);
      const sep = document.createElement("span");
      sep.className = "tape-sep";
      sep.textContent = "•";
      half.appendChild(sep);
    }
    const clone = half.cloneNode(true) as HTMLElement;
    this.track.replaceChildren(half, clone);
    this.itemSpans = [
      ...half.querySelectorAll<HTMLElement>(".tape-item"),
      ...clone.querySelectorAll<HTMLElement>(".tape-item"),
    ];

    requestAnimationFrame(() => {
      const width = half.scrollWidth;
      const duration = Math.max(width / SPEED_PX_PER_SECOND, MIN_DURATION_SECONDS);
      this.track.style.setProperty("--tape-duration", `${duration}s`);
      this.track.classList.remove("scrolling");
      void this.track.offsetWidth; // reflow restarts the animation
      this.track.classList.add("scrolling");
    });
  }
}
