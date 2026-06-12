import { sportIcon } from "../icons";
import { register } from "./registry";

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!,
  );
}

function gameRow(game: any, index: number): string {
  const live = game.state === "in";
  const pre = game.state === "pre";
  const status = pre
    ? new Date(game.start).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
    : escapeHtml(game.detail);
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
    el.innerHTML = `<div class="detail sports-detail">
      <div class="detail-meta"><span class="detail-icon">${sportIcon(item.sport, item.league)}</span>${escapeHtml(item.league)} · ${escapeHtml(item.detail)}</div>
      <div class="detail-big">
        ${escapeHtml(item.away?.name)} ${pre ? "" : escapeHtml(item.away?.score ?? "")}
        <span class="game-at">@</span>
        ${escapeHtml(item.home?.name)} ${pre ? "" : escapeHtml(item.home?.score ?? "")}
      </div>
    </div>`;
  },
});
