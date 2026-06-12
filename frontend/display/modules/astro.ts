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

register({
  id: "astro",
  renderStage(el, data) {
    const hours: any[] = data?.hours ?? [];
    const moon = data?.moon ?? {};
    const targets: string[] = data?.targets ?? [];
    const cloudStrip = hours
      .map((h) => {
        const cover = h.total ?? 100;
        const clear = 100 - cover;
        return `<div class="astro-hour" title="${cover}% cloud">
          <div class="astro-hour-bar" style="height:${Math.max(clear, 4)}%"
            data-quality="${cover < 25 ? "good" : cover < 60 ? "ok" : "bad"}"></div>
          <span class="astro-hour-label">${hourLabel(h.time)}</span>
        </div>`;
      })
      .join("");
    el.innerHTML = `<div class="astro-layout">
      <div class="astro-main">
        <div class="astro-head">
          <span class="astro-title">Sky tonight</span>
          <span class="astro-avg">${data?.avg_cloud != null ? `${data.avg_cloud}% avg cloud` : ""}</span>
        </div>
        <div class="astro-strip">${cloudStrip || '<div class="empty">No forecast</div>'}</div>
        <div class="astro-sun">
          ${data?.sunset ? `sunset ${new Date(data.sunset).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}` : ""}
          ${data?.sunrise ? ` · sunrise ${new Date(data.sunrise).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}` : ""}
        </div>
      </div>
      <div class="astro-side">
        <div class="astro-moon">
          <div class="astro-moon-pct">${moon.illumination ?? "?"}%</div>
          <div class="astro-moon-phase">${escapeHtml(moon.phase ?? "")}</div>
        </div>
        <div class="astro-targets">
          ${targets.map((t) => `<div class="astro-target">${escapeHtml(t)}</div>`).join("")}
        </div>
      </div>
    </div>`;
  },
});
