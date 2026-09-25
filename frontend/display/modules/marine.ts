// Beach & tides: the tide curve through the next day with where the water is
// now, the next highs and lows, and the beach tiles (water temperature, waves,
// UV). Every time in the payload is a UTC ISO string, so parsing is exact.
import { register } from "./registry";

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!,
  );
}

const GRAPH_W = 1000;
const GRAPH_H = 300;

function clock(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function hourLabel(ms: number): string {
  return new Date(ms).toLocaleTimeString([], { hour: "numeric" });
}

/** WHO UV bands. */
function uvBand(uv: number | null | undefined): { key: string; text: string } {
  if (uv == null) return { key: "none", text: "" };
  if (uv < 3) return { key: "low", text: "low" };
  if (uv < 6) return { key: "moderate", text: "moderate" };
  if (uv < 8) return { key: "high", text: "high" };
  if (uv < 11) return { key: "very", text: "very high" };
  return { key: "extreme", text: "extreme" };
}

function tideGraph(tide: any): string {
  const curve: any[] = (tide?.curve ?? []).filter((p: any) => p?.t && p?.ft != null);
  if (curve.length < 2) return "";
  const t0 = Date.parse(curve[0].t);
  const t1 = Date.parse(curve[curve.length - 1].t);
  const heights = curve.map((p) => p.ft);
  const lo = Math.min(...heights);
  const hi = Math.max(...heights);
  const span = hi - lo || 1;
  const x = (ms: number) => ((ms - t0) / (t1 - t0)) * GRAPH_W;
  const y = (ft: number) => 34 + (1 - (ft - lo) / span) * (GRAPH_H - 68);
  const points = curve.map((p) => `${x(Date.parse(p.t)).toFixed(1)},${y(p.ft).toFixed(1)}`).join(" ");

  // Highs and lows inside the window, labelled in HTML over the box (the svg
  // stretches with preserveAspectRatio="none", which would squash text).
  const tags = (tide.events ?? [])
    .filter((e: any) => {
      const ms = Date.parse(e.t);
      return ms >= t0 && ms <= t1;
    })
    .map((e: any) => {
      const ms = Date.parse(e.t);
      const lx = Math.min(95, Math.max(5, (x(ms) / GRAPH_W) * 100));
      const ly = (y(e.ft) / GRAPH_H) * 100;
      const high = e.type === "H";
      return `<span class="mar-tag ${high ? "high" : "low"}" style="left:${lx.toFixed(1)}%;top:${ly.toFixed(1)}%">
        <b>${high ? "High" : "Low"} ${Number(e.ft).toFixed(1)} ft</b>${escapeHtml(clock(e.t))}</span>`;
    })
    .join("");

  // Where the water is now: a line in the svg, the dot in HTML so it stays round.
  const now = Date.now();
  let nowLine = "";
  let nowDot = "";
  if (now >= t0 && now <= t1 && tide.now_ft != null) {
    const nx = x(now);
    nowLine = `<line class="mar-now-line" x1="${nx.toFixed(1)}" y1="0" x2="${nx.toFixed(1)}" y2="${GRAPH_H}"/>`;
    nowDot = `<span class="mar-now-dot" style="left:${((nx / GRAPH_W) * 100).toFixed(1)}%;top:${(
      (y(tide.now_ft) / GRAPH_H) * 100
    ).toFixed(1)}%"></span>`;
  }

  // Tick every 6 hours, on the hour.
  const ticks: string[] = [];
  const first = Math.ceil(t0 / 3_600_000) * 3_600_000;
  for (let ms = first; ms <= t1; ms += 3_600_000) {
    if (new Date(ms).getHours() % 6 !== 0) continue;
    const pct = Math.min(97, Math.max(3, (x(ms) / GRAPH_W) * 100));
    ticks.push(`<span style="left:${pct.toFixed(1)}%">${hourLabel(ms)}</span>`);
  }

  return `<div class="mar-graph-box">
      <svg class="mar-graph" viewBox="0 0 ${GRAPH_W} ${GRAPH_H}" preserveAspectRatio="none">
        <defs><linearGradient id="mar-grad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" class="mar-fill-top"/>
          <stop offset="100%" class="mar-fill-bottom"/>
        </linearGradient></defs>
        <polygon fill="url(#mar-grad)" stroke="none" points="0,${GRAPH_H} ${points} ${GRAPH_W},${GRAPH_H}"/>
        <polyline class="mar-line" points="${points}"/>
        ${nowLine}
      </svg>${tags}${nowDot}
    </div>
    <div class="mar-hour-labels">${ticks.join("")}</div>`;
}
function tile(label: string, value: string, sub: string, extra = ""): string {
  return `<div class="mar-tile" ${extra}>
    <div class="mar-tile-label">${label}</div>
    <div class="mar-tile-value">${value}</div>
    <div class="mar-tile-sub">${sub}</div>
  </div>`;
}

register({
  id: "marine",
  renderStage(el, data) {
    const tide = data?.tide ?? {};
    const next = tide.next;
    const station = data?.station ?? {};
    const rising = tide.rising;
    const headline =
      tide.now_ft != null
        ? `Tide ${rising == null ? "" : rising ? "rising" : "falling"} · ${Number(tide.now_ft).toFixed(1)} ft`
        : "Tides";
    const nextLine = next
      ? `${next.type === "H" ? "high" : "low"} ${escapeHtml(clock(next.t))} · ${Number(next.ft).toFixed(1)} ft`
      : "";

    const waves = data?.waves;
    const uv = data?.uv ?? {};
    const band = uvBand(uv.max ?? uv.now);
    const after = (tide.events ?? []).filter((e: any) => Date.parse(e.t) > Date.now());
    const following = after[1];

    const tiles = [
      tile(
        "Water",
        data?.water_f != null ? `${Math.round(data.water_f)}°` : "—",
        data?.water_source === "station" ? escapeHtml(station.name ?? "gauge") : "model estimate",
      ),
      tile(
        "Waves",
        waves ? `${Number(waves.ft).toFixed(1)} ft` : "—",
        waves
          ? `${waves.period_s != null ? `${Math.round(waves.period_s)} s` : ""}${
              waves.from ? ` · from ${escapeHtml(waves.from)}` : ""
            }`
          : "",
      ),
      tile(
        "UV",
        uv.now != null ? String(Math.round(uv.now)) : "—",
        uv.max != null ? `max ${Math.round(uv.max)} · ${band.text}` : "",
        `data-uv="${band.key}"`,
      ),
      following
        ? tile(
            following.type === "H" ? "Then high" : "Then low",
            escapeHtml(clock(following.t)),
            `${Number(following.ft).toFixed(1)} ft`,
            `data-kind="time"`,
          )
        : "",
    ].join("");

    el.innerHTML = `<div class="mar-stage">
      <div class="mar-main">
        <div class="mar-head">
          <span class="mar-headline">${escapeHtml(headline)}</span>
          <span class="mar-next">${nextLine ? `next ${nextLine}` : ""}</span>
        </div>
        ${tideGraph(tide)}
      </div>
      <div class="mar-tiles">${tiles}</div>
    </div>`;
  },
});
