import { GAME_ICONS } from "../icons";
import { register } from "./registry";

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!,
  );
}

/** Same style as the collector's tape text (wardogs.compact): 1.70M, 177K, 48.2K. */
function compact(n: number | null | undefined): string {
  if (n == null) return "—";
  const size = Math.abs(n);
  if (size >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (size >= 1e5) return `${Math.round(n / 1e3)}K`;
  if (size >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
  return String(Math.round(n));
}

const whole = (n: number | null | undefined): string => (n == null ? "—" : n.toLocaleString("en-US"));
const money = (n: number | null | undefined): string => (n == null ? "—" : `$${whole(n)}`);

function rank(r: any, ofTotal = true): string {
  if (!r?.rank) return "";
  return ofTotal && r.total ? `#${r.rank} <i>of ${whole(r.total)}</i>` : `#${r.rank}`;
}

function pct(value: number | null | undefined, label?: string): string {
  if (value == null) return "";
  const tone = value > 0 ? "up" : value < 0 ? "down" : "";
  const arrow = value > 0 ? "▲" : value < 0 ? "▼" : "";
  return `<span class="wd-pct ${tone}">${label ? `<i>${label}</i> ` : ""}${arrow} ${Math.abs(value).toFixed(1)}%</span>`;
}

/** "synced 12:49 PM", with the weekday once it isn't today. Absolute on
 *  purpose: the page only re-renders when the collector republishes. */
function synced(iso: string | null | undefined): string {
  if (!iso) return "";
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "";
  const time = at.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const today = at.toDateString() === new Date().toDateString();
  return `synced ${today ? "" : `${at.toLocaleDateString([], { weekday: "short" })} `}${time}`;
}

let gradSeq = 0;

/** 30 daily gold prices on a 100×28 box, min-max scaled: the price swings by
 *  multiples, so a zero-based scale would flatten the recent month. */
function spark(values: number[]): string {
  if (!values || values.length < 2) return "";
  const lo = Math.min(...values);
  const span = Math.max(1, Math.max(...values) - lo);
  const pts = values
    .map((v, i) => `${((i / (values.length - 1)) * 100).toFixed(1)},${(26 - ((v - lo) / span) * 24).toFixed(1)}`)
    .join(" ");
  const id = `wd-grad-${++gradSeq}`;
  return `<svg class="wd-spark" viewBox="0 0 100 28" preserveAspectRatio="none">
    <defs><linearGradient id="${id}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" class="wd-fill-top"/><stop offset="100%" class="wd-fill-bottom"/>
    </linearGradient></defs>
    <polygon fill="url(#${id})" stroke="none" points="0,28 ${pts} 100,28"/>
    <polyline class="wd-line" points="${pts}"/>
  </svg>`;
}

function seasonChip(data: any): string {
  if (data.season == null) return "";
  return `<span class="wd-chip ${data.season_final ? "final" : ""}">SEASON ${data.season}${
    data.season_final ? " · FINAL" : ""
  }</span>`;
}

function note(data: any): string {
  if (data.hidden) return "Profile hidden on wdstats";
  if (data.pending) return "First wdstats sync pending";
  if (data.sync_error) return `wdstats: ${data.sync_error}`;
  if (data.sync_stale) return "wdstats tracking paused — sign in there again";
  if (data.season_final && data.current_season)
    return `Season ${data.current_season} started — no S${data.current_season} XP yet`;
  return "";
}

function hero(data: any): string {
  const p = data.player ?? {};
  const gold = data.gold;
  const msg = note(data);
  return `<div class="wd-card wd-hero ${data.level_up ? "up" : ""}">
    <div class="wd-who">
      ${p.avatar ? `<img class="wd-avatar" src="${escapeHtml(p.avatar)}" alt="" onerror="this.remove()">` : ""}
      <div class="wd-who-text">
        <div class="wd-name">${escapeHtml(p.name)}</div>
        ${seasonChip(data)}
      </div>
    </div>
    <div class="wd-level-label">Wardog level</div>
    <div class="wd-level-row">
      <span class="wd-level">${data.level ?? "—"}</span>
      <span class="wd-rank">${rank(data.ranks?.level)}</span>
    </div>
    <div class="wd-xp"><b>${whole(data.xp)}</b> XP <span class="wd-rank">${rank(data.ranks?.xp, false)}</span></div>
    <div class="wd-mini">
      ${gold ? `<span class="wd-mini-gold">${GAME_ICONS["wd-gold"]}${money(gold.price)} ${pct(gold.pct_1d)}</span>` : ""}
      ${data.cash?.amount != null ? `<span>Cash ${money(data.cash.amount)}</span>` : ""}
    </div>
    ${msg ? `<div class="wd-note">${escapeHtml(msg)}</div>` : ""}
    <div class="wd-synced">${escapeHtml(synced(data.synced_at))}</div>
  </div>`;
}

/** Bars scaled to the top class: absolute XP would leave Assault at 9 a
 *  sliver beside Pilot at 32 either way, but the top class always reads full. */
function classes(rows: any[]): string {
  const top = Math.max(1, ...rows.map((c) => c.xp ?? 0));
  return `<div class="wd-card wd-classes">
    <div class="wd-title">Classes</div>
    <div class="wd-class-rows">${rows
      .map(
        (c) => `<div class="wd-class ${c.up ? "up" : ""}">
          <span class="wd-class-icon">${GAME_ICONS[c.icon] ?? GAME_ICONS.wardogs}</span>
          <span class="wd-class-name">${escapeHtml(c.label)}</span>
          <span class="wd-class-level"><i>LVL</i> ${c.level ?? "—"}</span>
          <span class="wd-bar"><span style="width:${Math.min(100, ((c.xp ?? 0) / top) * 100).toFixed(1)}%"></span></span>
          <span class="wd-class-xp">${compact(c.xp)}</span>
          <span class="wd-class-rank">${rank(c, false)}</span>
        </div>`,
      )
      .join("")}</div>
  </div>`;
}

function goldCard(gold: any): string {
  if (!gold) return "";
  const range =
    gold.low30 != null && gold.high30 != null ? `30-day ${money(gold.low30)} – ${money(gold.high30)}` : "";
  return `<div class="wd-card wd-gold">
    <div class="wd-title">${GAME_ICONS["wd-gold"]} Gold bar price</div>
    <div class="wd-gold-row">
      <span class="wd-gold-price">${money(gold.price)}</span>
      ${pct(gold.pct_1d)}
    </div>
    ${spark(gold.spark ?? [])}
    <div class="wd-gold-foot">${pct(gold.pct_7d, "7d")} ${pct(gold.pct_30d, "30d")}
      ${range ? `<span class="wd-range">${range}</span>` : ""}</div>
  </div>`;
}

function wallet(data: any): string {
  const bars = data.gold_bars ?? {};
  const gain = data.gain ?? {};
  // Only real gains: "+0 today" on a quiet day (or a finished season) is noise.
  const gains = [
    gain.week ? `<b>+${compact(gain.week)}</b> XP this week` : "",
    gain.today ? `<b>+${compact(gain.today)}</b> today` : "",
  ].filter(Boolean);
  const unlocks = `${whole(data.unlocks)}${data.cosmetics ? ` <i>+ ${data.cosmetics} cosmetics</i>` : ""}`;
  return `<div class="wd-card wd-wallet">
    <div class="wd-stat"><span class="wd-stat-k">Cash</span><span class="wd-stat-v">${money(data.cash?.amount)}</span><span class="wd-stat-r">${rank(data.cash, false)}</span></div>
    <div class="wd-stat"><span class="wd-stat-k">Gold bars</span><span class="wd-stat-v">${whole(bars.count)}${
      bars.value ? ` <i>≈ $${compact(bars.value)}</i>` : ""
    }</span><span class="wd-stat-r">${rank(bars, false)}</span></div>
    <div class="wd-stat wd-tap" ${data.rarest?.length ? `data-detail="rarest"` : ""}><span class="wd-stat-k">Unlocks</span><span class="wd-stat-v">${unlocks}</span><span class="wd-stat-r">${rank(data.ranks?.unlocks, false)}</span></div>
    ${gains.length ? `<div class="wd-gain">${gains.join(" · ")}</div>` : ""}
  </div>`;
}

register({
  id: "wardogs",
  renderStage(el, data) {
    const d = data ?? {};
    el.innerHTML = `<div class="wd-stage"><div class="wd-layout">
      ${hero(d)}
      ${classes((d.classes ?? []).slice(0, 6))}
      <div class="wd-side">
        ${goldCard(d.gold)}
        ${wallet(d)}
      </div>
    </div></div>`;
  },
  getDetailItem(stage, key) {
    if (key !== "rarest" || !stage?.rarest?.length) return undefined;
    return { rarest: stage.rarest, tracked: stage.tracked, name: stage.player?.name };
  },
  renderDetail(el, item: any) {
    if (!item) return;
    const tracked = item.tracked as number | null;
    el.innerHTML = `<div class="detail wd-detail">
      <div class="detail-title">Rarest unlocks · ${escapeHtml(item.name)}</div>
      <div class="wd-rare-rows">${(item.rarest as any[])
        .map(
          (r) => `<div class="wd-rare">
            <span class="wd-rare-name">${escapeHtml(r.name)}</span>
            <span class="wd-rare-cat">${escapeHtml(r.category)}</span>
            <span class="wd-rare-own">owned by <b>${whole(r.owners)}</b>${tracked ? ` of ${whole(tracked)}` : ""}</span>
            <span class="wd-rare-pct">${
              tracked && r.owners != null ? `${Math.max(1, Math.ceil((r.owners * 100) / tracked))}%` : ""
            }</span>
          </div>`,
        )
        .join("")}</div>
    </div>`;
  },
});
