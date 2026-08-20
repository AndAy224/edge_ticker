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

function gameRow(game: any): string {
  const live = game.state === "in";
  const pre = game.state === "pre";
  const status = pre ? gameTime(game.start) : escapeHtml(game.detail);
  return `<div class="game-row ${live ? "live" : ""} ${game.followed ? "followed" : ""}" data-detail="g:${escapeHtml(game.id)}">
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

/** How many rows fit each side of the now line. Stage layers are bottom-aligned
 *  and clip out the top, so these are budgets, not suggestions. */
function paneBudget(): number {
  const panes = Number(document.documentElement.dataset.panes ?? 1);
  return panes >= 3 ? 2 : panes === 2 ? 3 : 4;
}

/** Trim to the pane budget, but never at the cost of a followed team's game —
 *  the collector reserves the same seats, and the narrow panes would otherwise
 *  cull exactly the rows the board exists to show. */
function pickRows(games: any[], limit: number): any[] {
  const mine = games.filter((g) => g.followed).slice(0, limit);
  const rest = games.filter((g) => !g.followed);
  const filled = [...mine];
  for (const g of rest) {
    if (filled.length >= limit) break;
    filled.push(g);
  }
  // back into the order the caller handed us
  return games.filter((g) => filled.includes(g));
}

/** "<b>SUN</b> 16 AUG", shortened to "<b>SUN</b> 16" in narrow panes. */
function dayLabel(start: string | null, wide: boolean): string {
  if (!start) return "";
  const d = new Date(start);
  if (d.toDateString() === new Date().toDateString()) return "<b>TODAY</b>";
  const day = d.toLocaleDateString([], { weekday: "short" }).toUpperCase();
  const month = d.toLocaleDateString([], { month: "short" }).toUpperCase();
  return `<b>${escapeHtml(day)}</b> ${d.getDate()}${wide ? ` ${escapeHtml(month)}` : ""}`;
}

/** Clock only — the day already has its own column on the row. */
function kickoffTime(start: string | null): string {
  if (!start) return "TBD";
  return new Date(start).toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
  });
}

function timelineRow(g: any, nextUpId: unknown, wide: boolean, roomy: boolean): string {
  const pre = g.state === "pre";
  const live = g.state === "in";
  const nextUp = pre && g.followed && String(g.id) === String(nextUpId);

  // Only a finished game has a winner to grey the loser against.
  const a = Number(g.away?.score);
  const h = Number(g.home?.score);
  const decided = g.state === "post" && Number.isFinite(a) && Number.isFinite(h) && a !== h;
  const sideClass = (mine: number, theirs: number) =>
    decided ? (mine > theirs ? " win" : " lose") : "";

  const pts = (t: any) =>
    pre ? "" : `<span class="sb-pts">${escapeHtml(t?.score ?? "")}</span>`;
  // Crests sit on the outer edges of the matchup; ESPN omits the logo for some
  // minor-league and placeholder teams, so the row has to read without it.
  const crest = (t: any) =>
    t?.logo ? `<img class="sb-crest" src="${escapeHtml(t.logo)}" alt="">` : "";
  // Season record, riding the outer edge next to the crest. Wide panes only —
  // at panes=3 the matchup barely fits as it is.
  // Reads records.overall only, never the legacy `record` — the collector
  // clears `records` when show_records is off, and that has to actually hide it.
  const rec = (t: any) => {
    const value = t?.records?.overall;
    return wide && value ? `<span class="sb-rec">${escapeHtml(value)}</span>` : "";
  };
  // The betting line, its own column so it never shoves the matchup around.
  // Only pre-game: once the game starts the line is history.
  const odds = g.odds ?? null;
  // The over/under is the first thing to go when the column narrows — the
  // spread is the number that says who is favoured.
  const lineBits =
    pre && wide
      ? [odds?.details, roomy && odds?.over_under != null ? `O/U ${odds.over_under}` : ""]
      : [];
  const oddsText = lineBits.filter(Boolean).join(" · ");
  const oddsHtml = oddsText
    ? `<span class="sb-odds">${escapeHtml(oddsText)}</span>`
    : "";
  // Win probability as a labelled gauge rather than a two-tone split bar:
  // team colours are unreliable here (KC and TB are both red, and the split
  // vanishes), so the favourite is named in text and the bar is a plain meter.
  const wp = g.win_prob;
  const probHtml =
    wp && (pre || live) && Number.isFinite(Number(wp.home))
      ? (() => {
          const homeFav = Number(wp.home) >= Number(wp.away);
          const pct = Math.round(homeFav ? Number(wp.home) : Number(wp.away));
          const who = homeFav ? g.home?.abbrev : g.away?.abbrev;
          return `<span class="sb-wp">
            <span class="sb-wp-bar"><i style="width:${pct}%"></i></span>
            <span class="sb-wp-pct">${escapeHtml(who ?? "")} ${pct}%</span>
          </span>`;
        })()
      : "";
  const line = oddsHtml || probHtml ? `${oddsHtml}${probHtml}` : "";
  const status = pre
    ? kickoffTime(g.start)
    : live
      ? escapeHtml(g.detail ?? "")
      : wide
        ? escapeHtml(g.detail ?? "Final")
        : "F";
  // the badge rides my team's side of the matchup, home or away
  const tag = (side: string) =>
    nextUp && wide && g.followed_side === side
      ? '<span class="sbt-tag">NEXT UP</span>'
      : "";

  return `<div class="sb-row${live ? " live" : ""}${g.followed ? " followed" : ""}${
    nextUp ? " sbt-nextup" : ""
  }" data-detail="g:${escapeHtml(g.id)}">
    <span class="sb-when">${dayLabel(g.start, wide)}</span>
    <span class="sb-match">
      <span class="sb-side away${sideClass(a, h)}">${tag("away")}${crest(g.away)}${rec(
        g.away,
      )}<span class="sb-abbr">${escapeHtml(g.away?.abbrev ?? "")}</span>${pts(g.away)}</span>
      <span class="sb-at">at</span>
      <span class="sb-side home${sideClass(h, a)}">${pts(g.home)}<span class="sb-abbr">${escapeHtml(
        g.home?.abbrev ?? "",
      )}</span>${rec(g.home)}${crest(g.home)}${tag("home")}</span>
    </span>
    <span class="sb-line">${line}</span>
    <span class="sb-status">${live ? '<span class="live-dot"></span>' : ""}${status}</span>
  </div>`;
}

/** One board ordered by time: results above the now line, what's next below it. */
function timelineHtml(games: any[], nextUpId: unknown): string {
  if (!games.length) return `<div class="empty">No games in the next week</div>`;
  const limit = paneBudget();
  const panes = Number(document.documentElement.dataset.panes ?? 1);
  const wide = panes < 3;
  const roomy = panes < 2; // only the full-width pane fits the over/under too

  // The past group is newest-first in the DOM and reversed by CSS, so the
  // freshest result ends up hard against the now line.
  const past = pickRows(
    games.filter((g) => g.bucket === "recent" || g.state === "post"),
    limit,
  );
  const live = games.filter((g) => g.state === "in");
  const next = pickRows(
    games.filter((g) => g.bucket === "next" || g.state === "pre"),
    Math.max(0, limit - live.length),
  );

  const stamp = wide
    ? `NOW · ${new Date()
        .toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" })
        .toUpperCase()}`
    : "NOW";

  const group = (cls: string, rows: any[]) =>
    rows.length
      ? `<div class="sbt-group ${cls}">${rows
          .map((g) => timelineRow(g, nextUpId, wide, roomy))
          .join("")}</div>`
      : "";

  return `<div class="sports-timeline">
    ${group("sbt-past", past)}
    <div class="sbt-now">
      <span class="sbt-bar"></span>
      <span class="sbt-stamp">${escapeHtml(stamp)}</span>
      <span class="sbt-bar"></span>
    </div>
    ${group("sbt-next", [...live, ...next])}
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
    ${teamBlock(g.away, "away")}
    <div class="lg-center">
      <div class="lg-score">${escapeHtml(g.away?.score ?? "")} — ${escapeHtml(g.home?.score ?? "")}</div>
      <div class="lg-status"><span class="live-dot"></span>${escapeHtml(g.detail ?? "")}</div>
      ${compact ? "" : linescoreHtml(g)}
      <div class="lg-extra">${trackerExtra(g)}</div>
      ${compact ? "" : `<div class="lg-hint">tap for all scores</div>`}
    </div>
    ${teamBlock(g.home, "home")}
  </div>`;
}

/** Bases and count, or down & distance — whichever the sport has. */
function situationLine(g: any): string {
  const s = g.situation;
  if (!s) return "";
  if (s.down_distance) {
    const bits = [s.down_distance];
    if (s.red_zone) bits.push("RED ZONE");
    return bits.join("  ·  ");
  }
  const bits: string[] = [];
  if (s.balls != null && s.strikes != null) bits.push(`${s.balls}-${s.strikes}`);
  if (s.outs != null) bits.push(`${s.outs} out`);
  const on = [s.on_first && "1st", s.on_second && "2nd", s.on_third && "3rd"].filter(
    Boolean,
  );
  if (on.length) bits.push(`${on.join(", ")} occupied`);
  else if (s.outs != null) bits.push("bases empty");
  return bits.join("  ·  ");
}

/** Live tracker extras, straight off the payload.
 *
 *  This used to fetch /api/sports/detail on every render — a ~1MB MLB summary
 *  every 30s — for numbers the collector now ships on the stage payload. */
function trackerExtra(g: any): string {
  const rows: string[] = [];
  const prob = probBarHtml({ probability: statProb(g) }, g, g.win_prob?.source);
  if (prob) rows.push(prob);
  const line = situationLine(g) || g.situation?.last_play;
  if (line) rows.push(`<div class="lg-lastplay">${escapeHtml(line)}</div>`);
  return rows.join("");
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
        return;
    }
    el.innerHTML = timelineHtml(games, data?.next_up);
  },
  getDetailItem(stage, key) {
    if (key === "__list") return { __list: true, games: stage?.games ?? [] };
    // id-keyed, not index-keyed: the timeline reorders games, so an index would
    // open the wrong one.
    const id = key.startsWith("g:") ? key.slice(2) : key;
    return stage?.games?.find((g: any) => String(g.id) === id);
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
      ${teamBlock(item.away, "away")}
      <div class="gd-center">
        <div class="gd-league">${sportIcon(item.sport, item.league)}<span>${escapeHtml(item.league)}</span></div>
        <div class="gd-score">${escapeHtml(center)}</div>
        <div class="gd-status">${live ? '<span class="live-dot"></span>' : ""}${escapeHtml(item.detail ?? "")}</div>
        <div class="game-detail-extra"></div>
      </div>
      ${teamBlock(item.home, "home")}
    </div>`;
    enrichDetail(el, item, live);
  },
});

/** The split that actually applies to this game — home form for the home side. */
function splitRecord(t: any, side?: string): string {
  const records = t?.records;
  if (!records) return "";
  if (side === "home" && records.home) return `home ${records.home}`;
  if (side === "away" && records.road) return `away ${records.road}`;
  return "";
}

function teamBlock(t: any, side?: string): string {
  const split = splitRecord(t, side);
  return `<div class="gd-team">
    ${t?.logo ? `<img class="gd-logo" src="${escapeHtml(t.logo)}" alt="">` : ""}
    <div class="gd-abbrev">${escapeHtml(t?.abbrev)}</div>
    <div class="gd-name">${escapeHtml(t?.name)}</div>
    ${t?.record ? `<div class="gd-record">${escapeHtml(t.record)}</div>` : ""}
    ${split ? `<div class="gd-record gd-split">${escapeHtml(split)}</div>` : ""}
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

/** Collector win_prob -> the {home_pct, away_pct} shape probBarHtml expects. */
function statProb(g: any): { home_pct: number; away_pct: number } | null {
  const wp = g?.win_prob;
  if (!wp || wp.home == null || wp.away == null) return null;
  return { home_pct: Number(wp.home), away_pct: Number(wp.away) };
}

/** Where a probability came from, so a betting line never reads as a model. */
const PROB_SOURCE: Record<string, string> = {
  moneyline: "betting line",
  predictor: "ESPN projection",
  live: "live win probability",
};

function probBarHtml(d: any, item: any, source?: string): string {
  if (!d.probability) return "";
  const hp = d.probability.home_pct;
  const ap = d.probability.away_pct;
  const homeColor = item.home?.color ? `#${item.home.color}` : "#4da3ff";
  const awayColor = item.away?.color ? `#${item.away.color}` : "#8a94a3";
  const label = source && PROB_SOURCE[source]
    ? `<div class="gd-prob-src">${escapeHtml(PROB_SOURCE[source])}</div>`
    : "";
  return `<div class="gd-prob">
    <span class="gd-prob-pct">${Math.round(ap)}%</span>
    <span class="gd-prob-bar"><span style="width:${ap}%;background:${awayColor}"></span><span style="width:${hp}%;background:${homeColor}"></span></span>
    <span class="gd-prob-pct">${Math.round(hp)}%</span>
  </div>${label}`;
}

/** Odds as a full line: the spread, the total, and both prices. */
function oddsLine(o: any): string {
  if (!o) return "";
  const parts = [
    o.details,
    o.over_under != null ? `O/U ${o.over_under}` : "",
    o.moneyline?.away != null && o.moneyline?.home != null
      ? `ML ${signed(o.moneyline.away)} / ${signed(o.moneyline.home)}`
      : "",
  ];
  const line = parts.filter(Boolean).join("  ·  ");
  return line && o.provider ? `${line}   (${o.provider})` : line;
}

function signed(value: unknown): string {
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value ?? "");
  return n > 0 ? `+${n}` : String(n);
}

/** Win probability over the game, as an inline SVG (the panel has no emoji
 *  fonts and no charting library — every glyph here has to be drawn). */
function probSparkline(curve: number[], item: any): string {
  if (!curve || curve.length < 4) return "";
  const w = 260;
  const h = 46;
  const points = curve
    .map((value, i) => {
      const x = (i / (curve.length - 1)) * w;
      const y = h - (Math.max(0, Math.min(100, value)) / 100) * h;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  const homeColor = item.home?.color ? `#${escapeHtml(item.home.color)}` : "#4da3ff";
  return `<div class="gd-sparkline">
    <svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">
      <line x1="0" y1="${h / 2}" x2="${w}" y2="${h / 2}" class="gd-spark-mid"></line>
      <polyline points="${points}" fill="none" stroke="${homeColor}" stroke-width="2"
                vector-effect="non-scaling-stroke"></polyline>
    </svg>
    <span class="gd-spark-cap">${escapeHtml(item.home?.abbrev ?? "home")} win %</span>
  </div>`;
}

function standingsHtml(d: any, item: any): string {
  if (!d.standings?.length) return "";
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
            const trail = r.games_behind && r.games_behind !== "-" ? r.games_behind : r.streak;
            return `<div class="gd-standing-row ${mine ? "mine" : ""}">
              <span>${escapeHtml(r.team)}</span>
              <span>${escapeHtml(r.wins ?? "")}-${escapeHtml(r.losses ?? "")}${escapeHtml(ties)}</span>
              <span class="gd-standing-trail">${escapeHtml(trail ?? "")}</span>
            </div>`;
          })
          .join("")}
      </div>`,
    )
    .join("");
  return `<div class="gd-standings">${tables}</div>`;
}

/** Fetch the on-demand detail (odds, form, standings…) and fill the card.
 *
 *  Win probability prefers the stage payload — the collector keeps it current
 *  on the live cadence, and it carries the source label. */
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
      const main: string[] = [];

      const payloadProb = statProb(item);
      const prob = payloadProb
        ? probBarHtml({ probability: payloadProb }, item, item.win_prob?.source)
        : probBarHtml(d, item);
      if (prob) main.push(prob);
      if (d.prob_curve?.length) main.push(probSparkline(d.prob_curve, item));

      const odds = oddsLine(d.odds ?? item.odds);
      if (odds) main.push(`<div class="gd-line">${escapeHtml(odds)}</div>`);
      if (d.ats?.length) {
        const ats = d.ats.map((r: any) => `${r.abbrev} ${r.record}`).join("  ·  ");
        main.push(`<div class="gd-line">ATS: ${escapeHtml(ats)}</div>`);
      }
      if (live && (situationLine(item) || d.last_play)) {
        main.push(
          `<div class="gd-line gd-form">${escapeHtml(situationLine(item) || d.last_play)}</div>`,
        );
      }
      if (d.last_meeting?.text) {
        main.push(`<div class="gd-line">Last meeting: ${escapeHtml(d.last_meeting.text)}</div>`);
      }
      if (d.last_games?.away?.length) {
        main.push(`<div class="gd-line gd-form">${escapeHtml(item.away?.abbrev)}: ${escapeHtml(formLine(d.last_games.away))}</div>`);
      }
      if (d.last_games?.home?.length) {
        main.push(`<div class="gd-line gd-form">${escapeHtml(item.home?.abbrev)}: ${escapeHtml(formLine(d.last_games.home))}</div>`);
      }
      for (const side of ["away", "home"] as const) {
        const p = item[side]?.probable;
        if (p) {
          main.push(
            `<div class="gd-line gd-form">${escapeHtml(item[side]?.abbrev)} starter: ${escapeHtml(p.name)}${
              p.stat ? ` (${escapeHtml(p.stat)})` : ""
            }</div>`,
          );
        }
      }
      if (d.headline) {
        main.push(`<div class="gd-line gd-headline">${escapeHtml(d.headline)}</div>`);
      }
      const footer = [
        d.venue ?? item.venue?.name,
        d.broadcast ?? item.broadcast,
        d.weather,
        d.attendance ? `${Number(d.attendance).toLocaleString()} in` : "",
      ]
        .filter(Boolean)
        .join(" · ");
      if (footer) main.push(`<div class="gd-line gd-footer">${escapeHtml(footer)}</div>`);

      // Two columns: standings are the tallest block by far, and side-by-side
      // they cost width (which the card has) instead of height (which it does
      // not — a fully enriched card overflows a single column and clips).
      const standings = standingsHtml(d, item);
      extra.innerHTML = standings
        ? `<div class="gd-extra-cols"><div class="gd-col">${main.join(
            "",
          )}</div><div class="gd-col gd-col-side">${standings}</div></div>`
        : main.join("");
    })
    .catch(() => {});
}
