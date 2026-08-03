import qrcode from "qrcode-generator";
import { register } from "./registry";

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!,
  );
}

function minutesOld(published: string | null): number | null {
  if (!published) return null;
  return Math.max(0, Math.round((Date.now() - Date.parse(published)) / 60000));
}

function age(published: string | null): string {
  const minutes = minutesOld(published);
  if (minutes == null) return "";
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.round(minutes / 60);
  return hours < 24 ? `${hours}h` : `${Math.round(hours / 24)}d`;
}

// Same window the collector uses to flag a story as breaking on the tape.
const BREAKING_MINUTES = 15;

function isFresh(item: any): boolean {
  const minutes = minutesOld(item?.published);
  return minutes != null && minutes < BREAKING_MINUTES;
}

register({
  id: "news",
  renderStage(el, data) {
    // CSS caps what's visible per pane width, so keep every index aligned with
    // stage.items for data-detail / getDetailItem.
    const items: any[] = (data?.items ?? []).slice(0, 10);
    if (!items.length) {
      el.innerHTML = `<div class="empty">Waiting for headlines…</div>`;
      return;
    }
    // The lead is the newest story on the board; the digest keeps the
    // collector's round-robin order so the sources stay mixed.
    let leadIndex = 0;
    items.forEach((item, i) => {
      const a = minutesOld(item.published);
      const b = minutesOld(items[leadIndex].published);
      if (a != null && (b == null || a < b)) leadIndex = i;
    });
    const lead = items[leadIndex];
    const digest = items
      .map((item, i) => ({ item, i }))
      .filter(({ i }) => i !== leadIndex);
    el.innerHTML = `<div class="news-stage">
      <div class="news-lead${isFresh(lead) ? " fresh" : ""}" data-detail="${leadIndex}">
        <div class="lead-kicker">
          <span class="lead-source">${escapeHtml(lead.source)}</span>
          <span class="lead-age">${age(lead.published)} ago${isFresh(lead) ? " · breaking" : ""}</span>
        </div>
        <div class="lead-title">${escapeHtml(lead.title)}</div>
        ${lead.summary ? `<div class="lead-summary">${escapeHtml(lead.summary)}</div>` : ""}
        <div class="lead-foot">tap for the full story and a QR to read it on your phone</div>
      </div>
      <div class="news-digest">
        ${digest
          .map(
            ({ item, i }) => `<div class="news-row${isFresh(item) ? " fresh" : ""}" data-detail="${i}">
              <div class="news-title">${escapeHtml(item.title)}</div>
              <div class="news-meta">${escapeHtml(item.source)} · ${age(item.published)}</div>
            </div>`,
          )
          .join("")}
      </div>
    </div>`;
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
