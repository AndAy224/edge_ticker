import { WEATHER_ICONS } from "../icons";
import { register } from "./registry";

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!,
  );
}

// Toggled from config via main.ts (mirrors setSportsLiveMode).
let liveModeEnabled = true;
export function setFantasyLiveMode(enabled: boolean): void {
  liveModeEnabled = enabled;
}

function fmt(n: unknown): string {
  return n == null ? "—" : Number(n).toFixed(1);
}

function record(side: any): string {
  return side?.record ? `<span class="ff-rec">${escapeHtml(side.record)}</span>` : "";
}

/** At-a-glance warning chip: N starters need attention (injured / bye). */
function warnChip(n: unknown): string {
  const count = Number(n) || 0;
  if (count <= 0) return "";
  return `<span class="ff-warn" title="${count} starters need attention">${WEATHER_ICONS.warning}${count}</span>`;
}

function teamLogo(side: any, cls: string): string {
  const abbr = (side?.abbrev ?? "?").slice(0, 3);
  return side?.logo
    ? `<img class="${cls}" data-ff-fallback="${escapeHtml(abbr)}" src="${escapeHtml(side.logo)}" alt="">`
    : `<span class="${cls} ff-logo-fallback">${escapeHtml(abbr)}</span>`;
}

// Any logo that fails to load (e.g. an auth-only URL that 401s) is swapped for
// the team-abbrev chip. Captured at the document level since <img> error events
// don't bubble; gated on data-ff-fallback so only fantasy logos are touched.
document.addEventListener(
  "error",
  (e) => {
    const img = e.target as HTMLElement;
    if (!(img instanceof HTMLImageElement) || img.dataset.ffFallback == null) return;
    const span = document.createElement("span");
    span.className = `${img.className} ff-logo-fallback`;
    span.textContent = img.dataset.ffFallback;
    img.replaceWith(span);
  },
  true,
);

/** Win-probability split bar (my side highlighted by order). */
function probBar(m: any): string {
  const wp = m?.winProbability;
  if (!wp || m.state === "post") return "";
  const hp = wp.home_pct ?? 50;
  const ap = wp.away_pct ?? 50;
  return `<div class="ff-prob">
    <span class="ff-prob-pct">${Math.round(ap)}%</span>
    <span class="ff-prob-bar">
      <span class="ff-prob-away" style="width:${ap}%"></span>
      <span class="ff-prob-home" style="width:${hp}%"></span>
    </span>
    <span class="ff-prob-pct">${Math.round(hp)}%</span>
  </div>`;
}

function starterRow(s: any): string {
  const yet = s.yetToPlay ? "ff-yet" : "";
  const val = s.points != null ? fmt(s.points) : s.projected != null ? `${fmt(s.projected)}*` : "—";
  return `<div class="ff-starter ${yet}">
    <span class="ff-slot">${escapeHtml(s.slot)}</span>
    <span class="ff-pname">${escapeHtml(s.name)}</span>
    <span class="ff-ppts">${escapeHtml(val)}</span>
  </div>`;
}

function teamColumn(side: any, mine: boolean): string {
  if (!side) return `<div class="ff-col"><div class="ff-bye">BYE</div></div>`;
  const starters: any[] = side.starters ?? [];
  return `<div class="ff-col ${mine ? "ff-mine" : ""}">
    <div class="ff-col-head">
      ${teamLogo(side, "ff-col-logo")}
      <div class="ff-col-meta">
        <div class="ff-col-abbr">${escapeHtml(side.abbrev)}</div>
        ${record(side)}
      </div>
      <div class="ff-col-pts">${fmt(side.points)}</div>
    </div>
    ${side.projected != null ? `<div class="ff-col-proj">proj ${fmt(side.projected)}</div>` : ""}
    <div class="ff-lineup">${starters.map(starterRow).join("")}</div>
  </div>`;
}

/** Full-screen live tracker: my matchup, big scores, WP, both lineups. */
function liveTracker(data: any): string {
  const m = data.matchup;
  const mineHome = m.mineSide === "home";
  const me = mineHome ? m.home : m.away;
  const opp = mineHome ? m.away : m.home;
  return `<div class="ff-live" data-detail="matchup">
    <div class="ff-live-head">
      <span class="ff-live-league">${escapeHtml(data.meta?.league ?? "")} · Wk ${escapeHtml(data.meta?.week ?? "")}</span>
      <span class="ff-head-right">${warnChip(m.attention)}<span class="ff-live-dot"><span class="live-dot"></span>LIVE</span></span>
    </div>
    <div class="ff-live-score">
      <div class="ff-live-team ff-mine">${teamLogo(me, "ff-live-logo")}<div><div class="ff-live-abbr">${escapeHtml(me?.abbrev)}</div><div class="ff-live-name">${escapeHtml(me?.name)}</div></div></div>
      <div class="ff-live-nums"><span>${fmt(me?.points)}</span><span class="ff-live-dash">–</span><span>${fmt(opp?.points)}</span></div>
      <div class="ff-live-team ff-live-opp"><div><div class="ff-live-abbr">${escapeHtml(opp?.abbrev)}</div><div class="ff-live-name">${escapeHtml(opp?.name)}</div></div>${teamLogo(opp, "ff-live-logo")}</div>
    </div>
    ${probBar({ ...m, winProbability: mineHome ? m.winProbability : flipWp(m.winProbability) })}
    <div class="ff-live-proj">proj ${fmt(me?.projected)} — ${fmt(opp?.projected)}</div>
    <div class="ff-live-lineups">
      ${teamColumn(me, true)}
      ${teamColumn(opp, false)}
    </div>
    <div class="ff-hint">tap for full boxscore</div>
  </div>`;
}

function flipWp(wp: any): any {
  return wp ? { home_pct: wp.away_pct, away_pct: wp.home_pct } : wp;
}

function matchupCard(data: any): string {
  const m = data.matchup;
  if (!m) return "";
  const mineHome = m.mineSide === "home";
  const me = mineHome ? m.home : m.away ?? m.home;
  const opp = mineHome ? m.away : m.home;
  const stateTag =
    m.state === "in" ? `<span class="ff-live-dot"><span class="live-dot"></span>LIVE</span>`
    : m.state === "post" ? `<span class="ff-final">FINAL</span>`
    : `<span class="ff-pre">Wk ${escapeHtml(data.meta?.week ?? "")}</span>`;
  return `<div class="ff-matchup" data-detail="matchup">
    <div class="ff-matchup-head"><span>My Matchup</span><span class="ff-head-right">${warnChip(m.attention)}${stateTag}</span></div>
    <div class="ff-matchup-body">
      <div class="ff-m-team ff-mine">${teamLogo(me, "ff-m-logo")}
        <div class="ff-m-info"><div class="ff-m-abbr">${escapeHtml(me?.abbrev)}</div>${record(me)}</div>
        <div class="ff-m-pts">${fmt(me?.points)}</div>
      </div>
      <div class="ff-m-vs">vs</div>
      <div class="ff-m-team">
        <div class="ff-m-pts">${fmt(opp?.points)}</div>
        <div class="ff-m-info ff-right"><div class="ff-m-abbr">${escapeHtml(opp?.abbrev)}</div>${record(opp)}</div>
        ${teamLogo(opp, "ff-m-logo")}
      </div>
    </div>
    ${probBar({ ...m, winProbability: mineHome ? m.winProbability : flipWp(m.winProbability) })}
    ${
      me?.projected != null || opp?.projected != null
        ? `<div class="ff-m-proj">proj ${fmt(me?.projected)} — ${fmt(opp?.projected)}</div>`
        : ""
    }
  </div>`;
}

function standingsTable(data: any): string {
  const rows: any[] = data.standings ?? [];
  if (!rows.length) return "";
  return `<div class="ff-panel ff-standings">
    <div class="ff-panel-head">Standings</div>
    ${rows
      .map(
        (r) => `<div class="ff-st-row ${r.mine ? "ff-mine-row" : ""}" data-detail="team:${escapeHtml(r.teamId)}">
        <span class="ff-st-rank">${escapeHtml(r.rank)}</span>
        <span class="ff-st-name">${escapeHtml(r.abbrev)}</span>
        <span class="ff-st-rec">${escapeHtml(r.wins)}-${escapeHtml(r.losses)}${r.ties ? "-" + escapeHtml(r.ties) : ""}</span>
        <span class="ff-st-pf">${fmt(r.pointsFor)}</span>
      </div>`,
      )
      .join("")}
  </div>`;
}

function scoreboardPanel(data: any): string {
  const games: any[] = (data.scoreboard ?? []).filter((g: any) => g.away);
  if (!games.length) return "";
  return `<div class="ff-panel ff-scoreboard">
    <div class="ff-panel-head">Week ${escapeHtml(data.meta?.week ?? "")}</div>
    ${games
      .map((g) => {
        const live = g.state === "in";
        return `<div class="ff-sb-row ${g.mineSide ? "ff-mine-row" : ""}" data-detail="game:${escapeHtml(g.home.teamId)}">
          <span class="ff-sb-team">${escapeHtml(g.away.abbrev)}</span>
          <span class="ff-sb-pts">${fmt(g.away.points)}</span>
          <span class="ff-sb-sep">${live ? '<span class="live-dot"></span>' : g.state === "post" ? "F" : "@"}</span>
          <span class="ff-sb-pts">${fmt(g.home.points)}</span>
          <span class="ff-sb-team">${escapeHtml(g.home.abbrev)}</span>
        </div>`;
      })
      .join("")}
  </div>`;
}

function trendPanel(data: any): string {
  const t: any[] = data.trend ?? [];
  if (t.length < 2) return "";
  const pts = t.map((w) => w.points);
  const min = Math.min(...pts), max = Math.max(...pts), span = max - min || 1;
  const W = 360, H = 90;
  const x = (i: number) => (i / (t.length - 1)) * W;
  const y = (v: number) => H - 10 - ((v - min) / span) * (H - 20);
  const line = t.map((w, i) => `${x(i).toFixed(0)},${y(w.points).toFixed(0)}`).join(" ");
  const wl = t.map((w) => w.result).join("");
  const wins = t.filter((w) => w.result === "W").length;
  return `<div class="ff-panel ff-trend" data-detail="myteam">
    <div class="ff-panel-head">My Season · ${wins}-${t.length - wins}<span class="ff-tap">my team ›</span></div>
    <svg class="ff-trend-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
      <polyline points="${line}" class="ff-trend-line"/>
      ${t.map((w, i) => `<circle cx="${x(i).toFixed(0)}" cy="${y(w.points).toFixed(0)}" r="3" class="ff-trend-dot ${w.result === "W" ? "win" : "loss"}"/>`).join("")}
    </svg>
    <div class="ff-trend-wl">${wl.split("").map((c) => `<span class="ff-wl-${c}">${c}</span>`).join("")}</div>
  </div>`;
}

register({
  id: "fantasy",
  renderStage(el, data) {
    if (!data || (!data.matchup && !(data.standings ?? []).length)) {
      const wk = data?.meta?.week;
      el.innerHTML = `<div class="empty">${
        data?.meta?.league
          ? `${escapeHtml(data.meta.league)} — season starts soon`
          : "Waiting for fantasy data…"
      }${wk ? "" : ""}</div>`;
      return;
    }
    if (liveModeEnabled && data.matchup?.state === "in") {
      el.innerHTML = liveTracker(data);
      return;
    }
    el.innerHTML = `<div class="fantasy-stage">
      <div class="ff-left">
        ${matchupCard(data)}
        ${trendPanel(data)}
      </div>
      <div class="ff-right">
        ${standingsTable(data)}
        ${scoreboardPanel(data)}
      </div>
    </div>`;
  },
  getDetailItem(stage, key) {
    const meta = stage?.meta;
    if (!meta || typeof key !== "string") return null;
    if (key === "matchup") {
      const m = stage.matchup;
      if (!m) return null;
      const tid = m.mineSide === "home" ? m.home?.teamId : m.away?.teamId ?? m.home?.teamId;
      return { kind: "box", teamId: tid, meta };
    }
    if (key.startsWith("game:")) return { kind: "box", teamId: Number(key.slice(5)), meta };
    if (key === "myteam") return { kind: "team", teamId: stage.myTeam?.teamId, meta };
    if (key.startsWith("team:")) return { kind: "team", teamId: Number(key.slice(5)), meta };
    if (key.startsWith("player:")) {
      const [, pid, tid] = key.split(":");
      return { kind: "player", playerId: Number(pid), teamId: Number(tid), meta };
    }
    return null;
  },
  renderDetail(el, item: any) {
    if (item?.kind === "box") renderBox(el, item);
    else if (item?.kind === "team") renderTeam(el, item);
    else if (item?.kind === "player") renderPlayer(el, item);
  },
});

// ---- detail helpers --------------------------------------------------------

function metaText(meta: any): string {
  return `${escapeHtml(meta?.league ?? "")} · Week ${escapeHtml(meta?.week ?? "")}`;
}

function kickoff(ms: unknown): string {
  if (ms == null) return "";
  const d = new Date(Number(ms));
  return (
    d.toLocaleDateString([], { weekday: "short" }) +
    " " +
    d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
  );
}

/** Injury / bye badge. */
function injuryTag(p: any): string {
  if (p?.bye) return `<span class="ff-tag ff-tag-bye">BYE</span>`;
  if (!p?.injury) return "";
  const sev =
    p.injury === "OUT" || p.injury === "IR" || p.injury === "SUS" ? "out"
    : p.injury === "D" ? "doubt"
    : "q";
  return `<span class="ff-tag ff-tag-${sev}">${escapeHtml(p.injury)}</span>`;
}

function oppText(p: any): string {
  if (p?.bye) return "BYE";
  if (!p?.opp) return "";
  return `${escapeHtml(p.opp)}${p.kickoff ? " " + kickoff(p.kickoff) : ""}`;
}

function ptsText(p: any): string {
  return p?.points != null ? fmt(p.points) : p?.projected != null ? `${fmt(p.projected)}*` : "—";
}

function fetchDetail(item: any): Promise<any> {
  const m = item.meta;
  if (m?.leagueId == null || item.teamId == null) return Promise.resolve(null);
  const params = new URLSearchParams({
    league_id: String(m.leagueId),
    season: String(m.season),
    week: String(m.week),
    team_id: String(item.teamId),
  });
  return fetch(`/api/fantasy/detail?${params}`)
    .then((r) => (r.ok ? r.json() : null))
    .catch(() => null);
}

/** Matchup boxscore: both lineups with injury badge, opponent, points. */
function renderBox(el: HTMLElement, item: any): void {
  el.innerHTML = `<div class="detail ff-detail">
    <div class="ff-detail-head"><span>${metaText(item.meta)}</span><span class="ff-dim">loading…</span></div>
    <div data-ffbox></div>
  </div>`;
  const boxCol = (side: any) =>
    !side ? "" : `<div class="ff-box-col">
      <div class="ff-box-team">${escapeHtml(side.abbrev)} <strong>${fmt(side.points)}</strong>${
        side.projected != null ? ` <span class="ff-dim">proj ${fmt(side.projected)}</span>` : ""
      }</div>
      ${(side.players ?? [])
        .map(
          (p: any) => `<div class="ff-box-row ${p.bench ? "ff-bench" : ""}">
            <span class="ff-slot">${escapeHtml(p.pos || p.slot)}</span>
            <span class="ff-pname">${escapeHtml(p.name)}${injuryTag(p)}</span>
            <span class="ff-popp">${oppText(p)}</span>
            <span class="ff-ppts">${ptsText(p)}</span>
          </div>`,
        )
        .join("")}
    </div>`;
  fetchDetail(item).then((d) => {
    if (!d || d.error || !el.isConnected) return;
    const head = el.querySelector(".ff-detail-head");
    if (head) {
      head.innerHTML = `<span>${metaText(item.meta)}</span><span>${fmt(d.home?.points)} — ${fmt(d.away?.points)}</span>`;
    }
    const box = el.querySelector("[data-ffbox]");
    if (box) box.innerHTML = `<div class="ff-box">${boxCol(d.home)}${boxCol(d.away)}</div>`;
  });
}

/** Roster health board: injuries, byes, opponents, swap suggestions. */
function renderTeam(el: HTMLElement, item: any): void {
  el.innerHTML = `<div class="detail ff-board">
    <div class="ff-detail-head"><span>Roster · ${metaText(item.meta)}</span><span class="ff-dim">loading…</span></div>
    <div data-ffboard></div>
  </div>`;
  fetchDetail(item).then((d) => {
    if (!d || d.error || !el.isConnected) return;
    const head = el.querySelector(".ff-detail-head");
    if (head) head.innerHTML = `<span>Roster · ${metaText(item.meta)}</span><span class="ff-tap">tap to close</span>`;
    const wrap = el.querySelector("[data-ffboard]");
    if (!wrap) return;
    const attention = (d.attention ?? [])
      .map(
        (a: any) => `<div class="ff-att-row"><span class="ff-att-ico">${WEATHER_ICONS.warning}</span>
        <strong>${escapeHtml(a.out.name)}</strong> ${escapeHtml(a.out.reason)}${
          a.suggest
            ? ` → start <strong>${escapeHtml(a.suggest.name)}</strong> <span class="ff-dim">${escapeHtml(a.suggest.pos)} ${fmt(a.suggest.proj)}</span>`
            : ""
        }</div>`,
      )
      .join("");
    const rows = (d.roster ?? []).map((p: any) => playerRow(p, item.teamId)).join("");
    wrap.innerHTML = `${attention ? `<div class="ff-attention">${attention}</div>` : ""}<div class="ff-board-list">${rows}</div>`;
  });
}

function playerRow(p: any, teamId: number): string {
  const ir = p.lineupSlotId === 21 ? "ff-ir" : "";
  return `<div class="ff-board-row ${p.bench ? "ff-bench" : ""} ${ir}" data-detail="player:${escapeHtml(p.playerId)}:${escapeHtml(teamId)}">
    <span class="ff-slot">${escapeHtml(p.slot)}</span>
    <span class="ff-pname">${escapeHtml(p.name)}${injuryTag(p)}</span>
    <span class="ff-pteam">${escapeHtml(p.proTeam ?? "")}</span>
    <span class="ff-popp">${oppText(p)}</span>
    <span class="ff-ppts">${ptsText(p)}</span>
  </div>`;
}

/** Per-player card: identity, injury, ownership, this-week, season, sparkline. */
function renderPlayer(el: HTMLElement, item: any): void {
  el.innerHTML = `<div class="detail ff-pcard"><div data-ffpc class="ff-dim">loading…</div></div>`;
  const params = new URLSearchParams({
    league_id: String(item.meta.leagueId),
    season: String(item.meta.season),
    week: String(item.meta.week),
    player_id: String(item.playerId),
  });
  fetch(`/api/fantasy/player?${params}`)
    .then((r) => (r.ok ? r.json() : null))
    .then((d) => {
      if (!d || d.error || !el.isConnected) return;
      const wrap = el.querySelector("[data-ffpc]");
      if (!wrap) return;
      const own =
        d.percentOwned != null
          ? `Rostered ${fmt(d.percentOwned)}% · Started ${fmt(d.percentStarted)}%`
          : "";
      const thisWeek = d.bye
        ? "BYE"
        : d.opp
          ? `${escapeHtml(d.opp)}${d.kickoff ? " " + kickoff(d.kickoff) : ""}${d.projected != null ? ` · proj ${fmt(d.projected)}` : ""}`
          : "—";
      wrap.innerHTML = `
        <div class="ff-pc-head">
          <div>
            <div class="ff-pc-name">${escapeHtml(d.name)}${injuryTag(d)}</div>
            <div class="ff-pc-sub">${escapeHtml(d.pos ?? "")} · ${escapeHtml(d.proTeam ?? "")}${d.jersey ? " · #" + escapeHtml(d.jersey) : ""}</div>
          </div>
          <span class="ff-tap" data-detail="team:${escapeHtml(item.teamId)}">‹ back</span>
        </div>
        ${own ? `<div class="ff-pc-line">${own}</div>` : ""}
        <div class="ff-pc-line">This week: ${thisWeek}</div>
        <div class="ff-pc-line">Season: ${d.seasonTotal != null ? `${fmt(d.seasonTotal)} pts · ${fmt(d.seasonAvg)} avg` : "—"}</div>
        ${sparkline(d.weekly)}
      `;
    })
    .catch(() => {});
}

function sparkline(weekly: any[]): string {
  const pts = (weekly ?? []).filter((w) => w.points != null);
  if (pts.length < 2) return "";
  const vals = pts.map((w) => w.points);
  const min = Math.min(...vals), max = Math.max(...vals), span = max - min || 1;
  const W = 440, H = 70;
  const x = (i: number) => (i / (pts.length - 1)) * W;
  const y = (v: number) => H - 8 - ((v - min) / span) * (H - 16);
  const line = pts.map((w, i) => `${x(i).toFixed(0)},${y(w.points).toFixed(0)}`).join(" ");
  return `<div class="ff-pc-line">Weekly</div>
    <svg class="ff-spark-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
      <polyline points="${line}" class="ff-trend-line"/>
      ${pts.map((w, i) => `<circle cx="${x(i).toFixed(0)}" cy="${y(w.points).toFixed(0)}" r="2.5" class="ff-trend-dot win"/>`).join("")}
    </svg>`;
}
