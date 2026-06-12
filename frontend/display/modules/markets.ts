import { register } from "./registry";

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!,
  );
}

// Last rendered price per symbol, so only symbols whose price actually moved
// get the tick-flash animation (cards fully re-render on every stream publish).
const lastPrices = new Map<string, number>();

function sparkline(values: number[], prevClose: number | null, gradId: string): string {
  if (!values || values.length < 2) return "";
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const y = (v: number) => 26 - ((v - min) / span) * 24;
  const points = values
    .map((v, i) => `${((i / (values.length - 1)) * 100).toFixed(1)},${y(v).toFixed(1)}`)
    .join(" ");
  // Color by day change (vs prev close) when known, so the chart agrees with
  // the % badge; fall back to the window trend for spark-only data.
  const last = values[values.length - 1];
  const up = prevClose != null ? last >= prevClose : last >= values[0];
  const refY = prevClose != null && prevClose >= min && prevClose <= max ? y(prevClose) : null;
  return `<svg class="spark ${up ? "up" : "down"}" viewBox="0 0 100 28" preserveAspectRatio="none">
    <defs><linearGradient id="${gradId}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" class="spark-fill-top"/>
      <stop offset="100%" class="spark-fill-bottom"/>
    </linearGradient></defs>
    <polygon fill="url(#${gradId})" stroke="none" points="0,28 ${points} 100,28"/>
    ${refY != null ? `<line class="spark-ref" x1="0" y1="${refY.toFixed(1)}" x2="100" y2="${refY.toFixed(1)}"/>` : ""}
    <polyline points="${points}"/>
  </svg>`;
}

function rangeBar(quote: any): string {
  const { low, high, price } = quote;
  if (low == null || high == null || high <= low) return "";
  const pos = Math.min(100, Math.max(0, ((price - low) / (high - low)) * 100));
  return `<div class="quote-range">
    <span class="range-label">${formatPrice(low)}</span>
    <span class="range-track"><span class="range-marker" style="left: ${pos.toFixed(1)}%"></span></span>
    <span class="range-label">${formatPrice(high)}</span>
  </div>`;
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
          const last = lastPrices.get(q.symbol);
          const tick =
            last == null || last === q.price ? "" : q.price > last ? "tick-up" : "tick-down";
          lastPrices.set(q.symbol, q.price);
          return `<div class="quote-card ${up ? "up" : "down"}" data-detail="${i}">
            <div class="quote-head">
              <span class="quote-symbol">${escapeHtml(q.symbol)}</span>
              <span class="quote-change">${up ? "▲" : "▼"} ${Math.abs(q.pct ?? 0).toFixed(2)}%</span>
            </div>
            <div class="quote-price ${tick}">${formatPrice(q.price ?? 0)}
              <span class="quote-delta">${up ? "+" : "−"}${Math.abs(q.change ?? 0).toFixed(2)}</span>
            </div>
            ${sparkline(q.spark ?? [], q.prev_close ?? null, `spk-${i}`)}
            ${rangeBar(q)}
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
      ${sparkline(item.spark ?? [], item.prev_close ?? null, "spk-detail")}
      ${rangeBar(item)}
      <div class="detail-meta">
        ${item.open != null ? `Open ${formatPrice(item.open)}` : ""}
        ${item.prev_close != null ? ` · Prev close ${formatPrice(item.prev_close)}` : ""}
        ${escapeHtml(item.market_state ?? "")}
      </div>
    </div>`;
  },
});
