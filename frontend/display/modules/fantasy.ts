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

function teamLogo(side: any, cls: string): string {
  return side?.logo
    ? `<img class="${cls}" src="${escapeHtml(side.logo)}" alt="">`
    : `<span class="${cls} ff-logo-fallback">${escapeHtml((side?.abbrev ?? "?").slice(0, 3))}</span>`;
}

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
      <span class="ff-live-dot"><span class="live-dot"></span>LIVE</span>
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
    <div class="ff-matchup-head"><span>My Matchup</span>${stateTag}</div>
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
        (r) => `<div class="ff-st-row ${r.mine ? "ff-mine-row" : ""}">
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
        return `<div class="ff-sb-row ${g.mineSide ? "ff-mine-row" : ""}">
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
  return `<div class="ff-panel ff-trend">
    <div class="ff-panel-head">My Season · ${wins}-${t.length - wins}</div>
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
    if (key === "matchup" && stage?.matchup) {
      return { __box: true, matchup: stage.matchup, meta: stage.meta };
    }
    return null;
  },
  renderDetail(el, item: any) {
    if (!item?.__box) return;
    const m = item.matchup;
    const mineHome = m.mineSide === "home";
    const me = mineHome ? m.home : m.away ?? m.home;
    const opp = mineHome ? m.away : m.home;
    el.innerHTML = `<div class="detail ff-detail">
      <div class="ff-detail-head">
        <span>${escapeHtml(item.meta?.league ?? "")} · Week ${escapeHtml(item.meta?.week ?? "")}</span>
        <span>${fmt(me?.points)} — ${fmt(opp?.points)}</span>
      </div>
      <div class="ff-detail-cols">
        ${teamColumn(me, true)}
        ${teamColumn(opp, false)}
      </div>
      <div class="ff-detail-box" data-ffbox></div>
    </div>`;
    enrichBoxscore(el, item);
  },
});

/** Fetch the full boxscore (starters + bench, proj/actual) for the matchup. */
function enrichBoxscore(el: HTMLElement, item: any): void {
  const m = item.matchup;
  const teamId = m.mineSide === "home" ? m.home?.teamId : m.away?.teamId ?? m.home?.teamId;
  if (item.meta?.leagueId == null || teamId == null) return;
  const params = new URLSearchParams({
    league_id: String(item.meta.leagueId),
    season: String(item.meta.season),
    week: String(item.meta.week),
    team_id: String(teamId),
  });
  fetch(`/api/fantasy/detail?${params}`)
    .then((r) => (r.ok ? r.json() : null))
    .then((d) => {
      if (!d || d.error || !el.isConnected) return;
      const box = el.querySelector("[data-ffbox]");
      if (!box) return;
      const col = (side: any) =>
        side
          ? `<div class="ff-box-col">
              <div class="ff-box-team">${escapeHtml(side.abbrev)} <strong>${fmt(side.points)}</strong></div>
              ${(side.players ?? [])
                .map(
                  (p: any) => `<div class="ff-box-row ${p.bench ? "ff-bench" : ""}">
                    <span class="ff-slot">${escapeHtml(p.slot)}</span>
                    <span class="ff-pname">${escapeHtml(p.name)}</span>
                    <span class="ff-ppts">${p.points != null ? fmt(p.points) : p.projected != null ? fmt(p.projected) + "*" : "—"}</span>
                  </div>`,
                )
                .join("")}
            </div>`
          : "";
      box.innerHTML = `<div class="ff-box">${col(d.home)}${col(d.away)}</div>`;
    })
    .catch(() => {});
}
