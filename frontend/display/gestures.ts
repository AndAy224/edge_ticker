// Pointer-event gesture recognizer per PLAN §5:
// displacement > 60px within 300ms → swipe (dominant axis wins);
// ≤10px slop → tap on release; 500ms hold under slop → long-press.
// Native clicks are left alone so overlay buttons keep working.

export type SwipeDirection = "left" | "right" | "up" | "down";

export interface GestureHandlers {
  onSwipe(direction: SwipeDirection): void;
  onTap(target: EventTarget | null): void;
  onLongPress(target: EventTarget | null): void;
}

const SWIPE_DISTANCE_PX = 60;
const SWIPE_WINDOW_MS = 300;
const TAP_SLOP_PX = 10;
const LONG_PRESS_MS = 500;

export function attachGestures(el: HTMLElement, handlers: GestureHandlers): void {
  let startX = 0;
  let startY = 0;
  let startTime = 0;
  let tracking = false;
  let target: EventTarget | null = null;
  let longPressTimer = 0;

  el.addEventListener("pointerdown", (e: PointerEvent) => {
    tracking = true;
    startX = e.clientX;
    startY = e.clientY;
    startTime = performance.now();
    target = e.target;
    clearTimeout(longPressTimer);
    longPressTimer = window.setTimeout(() => {
      if (tracking) {
        tracking = false;
        handlers.onLongPress(target);
      }
    }, LONG_PRESS_MS);
  });

  el.addEventListener("pointermove", (e: PointerEvent) => {
    if (!tracking) return;
    const dx = e.clientX - startX;
    const dy = e.clientY - startY;
    const dist = Math.max(Math.abs(dx), Math.abs(dy));
    if (dist > TAP_SLOP_PX) clearTimeout(longPressTimer);
    if (dist > SWIPE_DISTANCE_PX && performance.now() - startTime <= SWIPE_WINDOW_MS) {
      tracking = false;
      const direction: SwipeDirection =
        Math.abs(dx) >= Math.abs(dy)
          ? dx > 0
            ? "right"
            : "left"
          : dy > 0
            ? "down"
            : "up";
      handlers.onSwipe(direction);
    }
  });

  el.addEventListener("pointerup", (e: PointerEvent) => {
    clearTimeout(longPressTimer);
    if (!tracking) return;
    tracking = false;
    const dx = e.clientX - startX;
    const dy = e.clientY - startY;
    if (Math.max(Math.abs(dx), Math.abs(dy)) <= TAP_SLOP_PX) {
      handlers.onTap(target);
    }
  });

  el.addEventListener("pointercancel", () => {
    clearTimeout(longPressTimer);
    tracking = false;
  });
}
