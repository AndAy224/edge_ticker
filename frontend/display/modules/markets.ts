import { register } from "./registry";

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!,
  );
}

function sparkline(values: number[]): string {
  if (!values || values.length < 2) return "";
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const points = values
    .map((v, i) => {
      const x = (i / (values.length - 1)) * 100;
      const y = 26 - ((v - min) / span) * 24;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  const up = values[values.length - 1] >= values[0];
  return `<svg class="spark ${up ? "up" : "down"}" viewBox="0 0 100 28" preserveAspectRatio="none"><polyline points="${points}" /></svg>`;
}

function formatPrice(price: number): string {
  return price.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

register({
  id: "markets",
  renderStage(el, data) {
    const quotes: any[] = data?.quotes ?? [];
    el.innerHTML =
      `<div class="markets-grid">` +
      quotes
        .map((q, i) => {
          const up = (q.change ?? 0) >= 0;
          return `<div class="quote-card ${up ? "up" : "down"}" data-detail="${i}">
            <div class="quote-head">
              <span class="quote-symbol">${escapeHtml(q.symbol)}</span>
              <span class="quote-change">${up ? "▲" : "▼"} ${Math.abs(q.pct ?? 0).toFixed(2)}%</span>
            </div>
            <div class="quote-price">${formatPrice(q.price ?? 0)}</div>
            ${sparkline(q.spark ?? [])}
          </div>`;
        })
        .join("") +
      `</div>`;
  },
  getDetailItem(stage, key) {
    return stage?.quotes?.[Number(key)];
  },
  renderDetail(el, item: any) {
    if (!item) return;
    const up = (item.change ?? 0) >= 0;
    el.innerHTML = `<div class="detail markets-detail ${up ? "up" : "down"}">
      <div class="detail-title">${escapeHtml(item.symbol)}</div>
      <div class="detail-big">${formatPrice(item.price ?? 0)}</div>
      <div class="detail-sub quote-change">${up ? "▲" : "▼"} ${Math.abs(item.change ?? 0).toFixed(2)} (${Math.abs(item.pct ?? 0).toFixed(2)}%)</div>
      ${sparkline(item.spark ?? [])}
      <div class="detail-meta">${escapeHtml(item.market_state ?? "")}</div>
    </div>`;
  },
});
