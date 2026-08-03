import { moonGlyph } from "../icons";
import { register } from "./registry";

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!,
  );
}

function hourLabel(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "numeric" });
}

function fmtTime(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

const CLEAR_ENOUGH = 40; // % cloud a frame can still be shot through

function quality(cover: number): string {
  return cover < 25 ? "good" : cover < 60 ? "ok" : "bad";
}

/** Which deck dominates — a low deck is opaque, high cirrus is still workable,
 *  so the column is shaded by this. The collector already ships all three. */
function dominantLayer(h: any): string {
  const low = h.low ?? 0;
  const mid = h.mid ?? 0;
  const high = h.high ?? 0;
  if (low >= mid && low >= high) return "low";
  return mid >= high ? "mid" : "high";
}

/** Longest run of dark hours at or under CLEAR_ENOUGH — the one thing an imager
 *  actually wants off this page. Twilight hours don't count: the sky isn't dark
 *  yet, however clear it is. */
function bestWindow(hours: any[], twilight: (h: any) => boolean): string {
  let bestFrom = -1;
  let bestLen = 0;
  let from = -1;
  let len = 0;
  hours.forEach((h, i) => {
    if ((h.total ?? 100) <= CLEAR_ENOUGH && !twilight(h)) {
      if (len === 0) from = i;
      len += 1;
      if (len > bestLen) {
        bestLen = len;
        bestFrom = from;
      }
    } else {
      len = 0;
    }
  });
  if (bestLen === 0) return "no clear window";
  const start = hourLabel(hours[bestFrom].time);
  const endHour = hours[bestFrom + bestLen - 1];
  const end = new Date(endHour.time);
  end.setHours(end.getHours() + 1);
  return `clear ${start} – ${end.toLocaleTimeString([], { hour: "numeric" })}`;
}

function targetRow(t: any): string {
  if (typeof t === "string") {
    // payload from a backend that predates target times
    return `<div class="astro-target">${escapeHtml(t)}</div>`;
  }
  let window = "";
  if (t.always_above) {
    window = "always ≥40°";
  } else if (t.above40_from) {
    window = `↑40° ${fmtTime(t.above40_from)} — ${fmtTime(t.above40_until)}`;
  } else if (t.max_alt != null) {
    window = `peaks ~${t.max_alt}°`;
  }
  const meridian = t.transit ? `meridian ${fmtTime(t.transit)}` : "";
  return `<div class="astro-target">
    <div class="astro-target-name">${escapeHtml(t.name)}</div>
    <div class="astro-target-times">${[window, meridian].filter(Boolean).join(" · ")}</div>
  </div>`;
}

register({
  id: "astro",
  renderStage(el, data) {
    const hours: any[] = data?.hours ?? [];
    const moon = data?.moon ?? {};
    const sunset: string = data?.sunset ?? "";
    const sunrise: string = data?.sunrise ?? "";
    // 5 targets fit the wide-pane side column beside the moon card; narrower
    // panes are capped further in CSS via [data-panes] nth-child.
    const targets: any[] = (data?.targets ?? []).slice(0, 5);
    // Every hour keeps a full-height track: cloud presses down from the top,
    // the clear remainder is the shootable part. Drawing only the clear part
    // (as this did) makes a clouded-out night render as an empty page.
    const isTwilight = (h: any): boolean =>
      Boolean((sunset && h.time < sunset) || (sunrise && h.time >= sunrise));
    const cloudStrip = hours
      .map((h) => {
        const cover = Math.min(100, Math.max(0, h.total ?? 100));
        return `<div class="astro-hour"${isTwilight(h) ? " data-twilight" : ""}>
          <div class="astro-hour-track">
            <div class="astro-hour-cloud" data-layer="${dominantLayer(h)}"
              style="height:${cover}%"></div>
            <div class="astro-hour-clear" data-quality="${quality(cover)}"
              style="height:${100 - cover}%"></div>
          </div>
          <span class="astro-hour-label">${hourLabel(h.time)}</span>
        </div>`;
      })
      .join("");
    const phase: string = moon.phase ?? "";
    const illumination: number = moon.illumination ?? 0;
    const window = hours.length ? bestWindow(hours, isTwilight) : "";
    el.innerHTML = `<div class="astro-layout">
      <div class="astro-main">
        <div class="astro-head">
          <span class="astro-title">Sky tonight</span>
          <span class="astro-avg">
            ${
              window
                ? `<span class="astro-window" data-clear="${window === "no clear window" ? "none" : "yes"}">${escapeHtml(window)}</span>`
                : ""
            }
            ${data?.avg_cloud != null ? `${data.avg_cloud}% avg cloud` : ""}
          </span>
        </div>
        <div class="astro-strip">${cloudStrip || '<div class="empty">No forecast</div>'}</div>
        <div class="astro-sun">
          ${data?.sunset ? `sunset ${fmtTime(data.sunset)}` : ""}
          ${data?.sunrise ? ` · sunrise ${fmtTime(data.sunrise)}` : ""}
        </div>
      </div>
      <div class="astro-side">
        <div class="astro-moon">
          <span class="astro-moon-glyph">${moonGlyph(illumination, phase.startsWith("Waxing") || phase === "First quarter")}</span>
          <div>
            <div class="astro-moon-pct">${illumination}%</div>
            <div class="astro-moon-phase">${escapeHtml(phase)}</div>
          </div>
        </div>
        <div class="astro-targets">
          ${targets.map(targetRow).join("")}
        </div>
      </div>
    </div>`;
  },
});
