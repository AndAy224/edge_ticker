import { sportIcon } from "../icons";
import { register } from "./registry";

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!,
  );
}

function gameTime(start: string | null): string {
  if (!start) return "";
  const d = new Date(start);
  const time = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  if (d.toDateString() === new Date().toDateString()) return time;
  const day = d.toLocaleDateString([], { weekday: "short" });
  return `${day} ${d.getMonth() + 1}/${d.getDate()} · ${time}`;
}

function gameRow(game: any, index: number): string {
  const live = game.state === "in";
  const pre = game.state === "pre";
  const status = pre ? gameTime(game.start) : escapeHtml(game.detail);
  return `<div class="game-row ${live ? "live" : ""} ${game.followed ? "followed" : ""}" data-detail="${index}">
    <span class="game-league">${sportIcon(game.sport, game.league)}<span>${escapeHtml(game.league)}</span></span>
    <span class="game-teams">
      <span class="team">${escapeHtml(game.away?.abbrev)} <strong>${pre ? "" : escapeHtml(game.away?.score ?? "")}</strong></span>
      <span class="game-at">@</span>
      <span class="team">${escapeHtml(game.home?.abbrev)} <strong>${pre ? "" : escapeHtml(game.home?.score ?? "")}</strong></span>
    </span>
    <span class="game-status">${live ? '<span class="live-dot"></span>' : ""}${status}</span>
  </div>`;
}

// Live game mode: render a full-screen tracker (instead of the list) while a
// followed team's game is in progress. Toggled from config via main.ts.
let liveModeEnabled = true;

export function setSportsLiveMode(enabled: boolean): void {
  liveModeEnabled = enabled;
}

function listHtml(games: any[]): string {
  return games.length
    ? `<div class="sports-list">${games.slice(0, 8).map(gameRow).join("")}</div>`
    : `<div class="empty">No games today</div>`;
}

/** "tonight 8:40 PM" for today, "Wed 8/6 · 8:00 PM" otherwise. */
function whenLine(start: string | null): string {
  if (!start) return "";
  const d = new Date(start);
  const time = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  if (d.toDateString() !== new Date().toDateString()) {
    return `${d.toLocaleDateString([], { weekday: "short" })} ${d.getMonth() + 1}/${d.getDate()} · ${time}`;
  }
  return `${d.getHours() >= 17 ? "tonight" : "today"} ${time}`;
}

function heroTeam(team: any, mine: boolean): string {
  return `<div class="sb-team${mine ? " mine" : ""}">
    ${team?.logo ? `<img class="sb-logo" src="${escapeHtml(team.logo)}" alt="">` : ""}
    <div class="sb-abbrev">${escapeHtml(team?.abbrev ?? "")}</div>
    ${team?.record ? `<div class="sb-record">${escapeHtml(team.record)}</div>` : ""}
  </div>`;
}

function heroHtml(g: any, index: number): string {
  const pre = g.state === "pre";
  const live = g.state === "in";
  const side = g.followed_side;
  // an away night for my team reads "at <them>", a home night "vs <them>"
  const opponent = side === "away" ? g.home : g.away;
  const context = side
    ? `${escapeHtml(g.league)} · ${side === "away" ? "at" : "vs"} ${escapeHtml(opponent?.name ?? "")}`
    : escapeHtml(g.league);
  const status = pre
    ? whenLine(g.start)
    : `${escapeHtml(g.away?.score ?? "")} — ${escapeHtml(g.home?.score ?? "")}`;
  return `<div class="sb-hero${g.followed ? " followed" : ""}" data-detail="${index}">
    <div class="sb-matchup">
      ${heroTeam(g.away, side === "away")}
      <span class="sb-at">${side === "home" ? "vs" : "@"}</span>
      ${heroTeam(g.home, side === "home")}
    </div>
    <div class="sb-status${live ? " live" : ""}">
      ${live ? '<span class="live-dot"></span>' : ""}${status}
    </div>
    <div class="sb-context">${pre ? context : `${escapeHtml(g.detail ?? "")} · ${context}`}</div>
  </div>`;
}

function boardRow(g: any, index: number): string {
  const pre = g.state === "pre";
  const live = g.state === "in";
  const right = pre ? gameTime(g.start) : escapeHtml(g.detail ?? "");
  const score = (t: any) => (pre ? "" : `<b>${escapeHtml(t?.score ?? "")}</b>`);
  return `<div class="sb-row${live ? " live" : ""}${g.followed ? " followed" : ""}" data-detail="${index}">
    <span class="sb-side">${escapeHtml(g.away?.abbrev ?? "")}${score(g.away)}</span>
    <span class="sb-vs">@</span>
    <span class="sb-side">${escapeHtml(g.home?.abbrev ?? "")}${score(g.home)}</span>
    <span class="sb-when">${live ? '<span class="live-dot"></span>' : ""}${right}</span>
  </div>`;
}

/** Lead with the followed game (the collector sorts it first), everything else
 *  compacts into the rail beside it. */
function boardHtml(games: any[]): string {
  if (!games.length) return `<div class="empty">No games today</div>`;
  let heroIndex = games.findIndex((g) => g.followed);
  if (heroIndex < 0) heroIndex = 0;
  const rest = games
    .map((game, i) => ({ game, i }))
    .filter(({ i }) => i !== heroIndex);
  return `<div class="sports-board">
    ${heroHtml(games[heroIndex], heroIndex)}
    <div class="sb-list">${rest.map(({ game, i }) => boardRow(game, i)).join("")}</div>
  </div>`;
}

function linescoreHtml(g: any): string {
  const away: any[] = g.away?.linescores ?? [];
  const home: any[] = g.home?.linescores ?? [];
  const n = Math.max(away.length, home.length);
  if (!n) return "";
  const head = Array.from({ length: n }, (_, i) => `<th>${i + 1}</th>`).join("");
  const row = (team: any, scores: any[]) =>
    `<tr><td class="lg-abbr">${escapeHtml(team?.abbrev)}</td>` +
    Array.from({ length: n }, (_, i) => `<td>${scores[i] ?? ""}</td>`).join("") +
    `<td class="lg-total">${escapeHtml(team?.score ?? "")}</td></tr>`;
  return `<table class="lg-linescore">
    <tr><th></th>${head}<th>T</th></tr>${row(g.away, away)}${row(g.home, home)}
  </table>`;
}

function gameTracker(g: any, compact: boolean): string {
  return `<div class="live-game ${compact ? "compact" : ""}" data-detail="__list">
    ${teamBlock(g.away)}
    <div class="lg-center">
      <div class="lg-score">${escapeHtml(g.away?.score ?? "")} — ${escapeHtml(g.home?.score ?? "")}</div>
      <div class="lg-status"><span class="live-dot"></span>${escapeHtml(g.detail ?? "")}</div>
      ${compact ? "" : linescoreHtml(g)}
      <div class="lg-extra" data-lg="${escapeHtml(g.id)}"></div>
      ${compact ? "" : `<div class="lg-hint">tap for all scores</div>`}
    </div>
    ${teamBlock(g.home)}
  </div>`;
}

/** Win probability + last play for a tracker, via the cached detail proxy. */
function enrichTracker(el: HTMLElement, item: any): void {
  const params = new URLSearchParams({
    sport: item.sport ?? "",
    league: item.league ?? "",
    event: String(item.id ?? ""),
  });
  fetch(`/api/sports/detail?${params}`)
    .then((r) => (r.ok ? r.json() : null))
    .then((d) => {
      if (!d || !el.isConnected) return;
      const extra = el.querySelector(`.lg-extra[data-lg="${CSS.escape(String(item.id))}"]`);
      if (!extra) return;
      const rows: string[] = [];
      const prob = probBarHtml(d, item);
      if (prob) rows.push(prob);
      if (d.last_play) rows.push(`<div class="lg-lastplay">${escapeHtml(d.last_play)}</div>`);
      extra.innerHTML = rows.join("");
    })
    .catch(() => {});
}

register({
  id: "sports",
  renderStage(el, data) {
    const games: any[] = data?.games ?? [];
    const live = liveModeEnabled
      ? games.filter((g) => g.followed && g.state === "in")
      : [];
    if (live.length) {
      const tracked = live.slice(0, 2);
      el.innerHTML =
        tracked.length === 1
          ? gameTracker(tracked[0], false)
          : `<div class="live-game-grid">${tracked.map((g) => gameTracker(g, true)).join("")}</div>`;
      for (const g of tracked) enrichTracker(el, g);
      return;
    }
    el.innerHTML = boardHtml(games);
  },
  getDetailItem(stage, key) {
    if (key === "__list") return { __list: true, games: stage?.games ?? [] };
    return stage?.games?.[Number(key)];
  },
  renderDetail(el, item: any) {
    if (!item) return;
    if (item.__list) {
      // Live mode peek: show the regular scores list as the "detail".
      el.innerHTML = listHtml(item.games ?? []);
      return;
    }
    const pre = item.state === "pre";
    const live = item.state === "in";
    const center = pre
      ? gameTime(item.start)
      : `${item.away?.score ?? ""} — ${item.home?.score ?? ""}`;
    el.innerHTML = `<div class="detail sports-detail-rich">
      ${teamBlock(item.away)}
      <div class="gd-center">
        <div class="gd-league">${sportIcon(item.sport, item.league)}<span>${escapeHtml(item.league)}</span></div>
        <div class="gd-score">${escapeHtml(center)}</div>
        <div class="gd-status">${live ? '<span class="live-dot"></span>' : ""}${escapeHtml(item.detail ?? "")}</div>
        <div class="game-detail-extra"></div>
      </div>
      ${teamBlock(item.home)}
    </div>`;
    enrichDetail(el, item, live);
  },
});

function teamBlock(t: any): string {
  return `<div class="gd-team">
    ${t?.logo ? `<img class="gd-logo" src="${escapeHtml(t.logo)}" alt="">` : ""}
    <div class="gd-abbrev">${escapeHtml(t?.abbrev)}</div>
    <div class="gd-name">${escapeHtml(t?.name)}</div>
    ${t?.record ? `<div class="gd-record">${escapeHtml(t.record)}</div>` : ""}
  </div>`;
}

function formLine(games: any[]): string {
  return games
    .map((g) =>
      [g.result, g.score, g.at_vs, g.opponent].filter(Boolean).join(" ").trim(),
    )
    .filter(Boolean)
    .join("  ·  ");
}

function probBarHtml(d: any, item: any): string {
  if (!d.probability) return "";
  const hp = d.probability.home_pct;
  const ap = d.probability.away_pct;
  const homeColor = item.home?.color ? `#${item.home.color}` : "#4da3ff";
  const awayColor = item.away?.color ? `#${item.away.color}` : "#8a94a3";
  return `<div class="gd-prob">
    <span class="gd-prob-pct">${Math.round(ap)}%</span>
    <span class="gd-prob-bar"><span style="width:${ap}%;background:${awayColor}"></span><span style="width:${hp}%;background:${homeColor}"></span></span>
    <span class="gd-prob-pct">${Math.round(hp)}%</span>
  </div>`;
}

/** Fetch the on-demand detail (win prob, odds, form…) and fill the card. */
function enrichDetail(el: HTMLElement, item: any, live: boolean): void {
  const params = new URLSearchParams({
    sport: item.sport ?? "",
    league: item.league ?? "",
    event: String(item.id ?? ""),
  });
  fetch(`/api/sports/detail?${params}`)
    .then((r) => (r.ok ? r.json() : null))
    .then((d) => {
      if (!d || !el.isConnected) return;
      const extra = el.querySelector(".game-detail-extra");
      if (!extra) return;
      const rows: string[] = [];
      const prob = probBarHtml(d, item);
      if (prob) rows.push(prob);
      if (d.odds) {
        const parts = [d.odds.details, d.odds.over_under != null ? `O/U ${d.odds.over_under}` : ""];
        rows.push(`<div class="gd-line">${escapeHtml(parts.filter(Boolean).join(" · "))}</div>`);
      }
      if (d.last_meeting?.text) {
        rows.push(`<div class="gd-line">Last meeting: ${escapeHtml(d.last_meeting.text)}</div>`);
      }
      if (d.last_games?.away?.length) {
        rows.push(`<div class="gd-line gd-form">${escapeHtml(item.away?.abbrev)}: ${escapeHtml(formLine(d.last_games.away))}</div>`);
      }
      if (d.last_games?.home?.length) {
        rows.push(`<div class="gd-line gd-form">${escapeHtml(item.home?.abbrev)}: ${escapeHtml(formLine(d.last_games.home))}</div>`);
      }
      if (live && d.last_play) {
        rows.push(`<div class="gd-line gd-form">${escapeHtml(d.last_play)}</div>`);
      }
      if (d.standings?.length) {
        const names = [item.home?.name, item.away?.name].filter(Boolean);
        const tables = d.standings
          .map(
            (g: any) => `<div class="gd-standing">
              <div class="gd-standing-head">${escapeHtml(g.header ?? "")}</div>
              ${g.rows
                .map((r: any) => {
                  const mine = names.some(
                    (n: string) => r.team && (n.includes(r.team) || r.team.includes(n)),
                  );
                  const ties = r.ties && r.ties !== "0" ? `-${r.ties}` : "";
                  return `<div class="gd-standing-row ${mine ? "mine" : ""}">
                    <span>${escapeHtml(r.team)}</span>
                    <span>${escapeHtml(r.wins ?? "")}-${escapeHtml(r.losses ?? "")}${escapeHtml(ties)}</span>
                  </div>`;
                })
                .join("")}
            </div>`,
          )
          .join("");
        rows.push(`<div class="gd-standings">${tables}</div>`);
      }
      const footer = [d.venue, d.broadcast].filter(Boolean).join(" · ");
      if (footer) rows.push(`<div class="gd-line gd-footer">${escapeHtml(footer)}</div>`);
      extra.innerHTML = rows.join("");
    })
    .catch(() => {});
}
