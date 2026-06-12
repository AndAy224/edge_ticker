import qrcode from "qrcode-generator";
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
        .slice(0, 10) // CSS caps what's visible per pane width (data-panes)
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
    let qrCard = "";
    if (item.link) {
      try {
        const qr = qrcode(0, "M");
        qr.addData(String(item.link));
        qr.make();
        // White card: dark modules are unscannable on the dark themes.
        qrCard = `<div class="news-qr">${qr.createSvgTag({ cellSize: 4, margin: 4 })}
          <span class="news-qr-hint">scan to read</span></div>`;
      } catch {
        // oversized/invalid URL — just skip the QR
      }
    }
    el.innerHTML = `<div class="detail news-detail">
      <div class="news-detail-text">
        <div class="detail-big">${escapeHtml(item.title)}</div>
        ${item.summary ? `<div class="news-summary">${escapeHtml(item.summary)}</div>` : ""}
        <div class="detail-meta">${escapeHtml(item.source)} · ${age(item.published)} ago</div>
      </div>
      ${qrCard}
    </div>`;
  },
});
