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

register({
  id: "sports",
  renderStage(el, data) {
    const games: any[] = data?.games ?? [];
    el.innerHTML = games.length
      ? `<div class="sports-list">${games.slice(0, 8).map(gameRow).join("")}</div>`
      : `<div class="empty">No games today</div>`;
  },
  getDetailItem(stage, key) {
    return stage?.games?.[Number(key)];
  },
  renderDetail(el, item: any) {
    if (!item) return;
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
      if (d.probability) {
        const hp = d.probability.home_pct;
        const ap = d.probability.away_pct;
        const homeColor = item.home?.color ? `#${item.home.color}` : "#4da3ff";
        const awayColor = item.away?.color ? `#${item.away.color}` : "#8a94a3";
        rows.push(`<div class="gd-prob">
          <span class="gd-prob-pct">${Math.round(ap)}%</span>
          <span class="gd-prob-bar"><span style="width:${ap}%;background:${awayColor}"></span><span style="width:${hp}%;background:${homeColor}"></span></span>
          <span class="gd-prob-pct">${Math.round(hp)}%</span>
        </div>`);
      }
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
      const footer = [d.venue, d.broadcast].filter(Boolean).join(" · ");
      if (footer) rows.push(`<div class="gd-line gd-footer">${escapeHtml(footer)}</div>`);
      extra.innerHTML = rows.join("");
    })
    .catch(() => {});
}
