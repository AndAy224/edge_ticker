import { register } from "./registry";

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!,
  );
}

function age(published: string | null): string {
  if (!published) return "";
  const minutes = Math.max(0, Math.round((Date.now() - Date.parse(published)) / 60000));
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.round(minutes / 60);
  return hours < 24 ? `${hours}h` : `${Math.round(hours / 24)}d`;
}

register({
  id: "news",
  renderStage(el, data) {
    const items: any[] = data?.items ?? [];
    el.innerHTML =
      `<div class="news-list">` +
      items
        .slice(0, 6)
        .map(
          (item, i) => `<div class="news-row" data-detail="${i}">
            <div class="news-title">${escapeHtml(item.title)}</div>
            <div class="news-meta">${escapeHtml(item.source)} · ${age(item.published)}</div>
          </div>`,
        )
        .join("") +
      `</div>`;
  },
  getDetailItem(stage, key) {
    return stage?.items?.[Number(key)];
  },
  renderDetail(el, item: any) {
    if (!item) return;
    el.innerHTML = `<div class="detail news-detail">
      <div class="detail-big">${escapeHtml(item.title)}</div>
      <div class="detail-meta">${escapeHtml(item.source)} · ${age(item.published)} ago</div>
    </div>`;
  },
});
