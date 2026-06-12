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
    el.innerHTML = `<div class="detail markets-detail markets-detail-rich ${up ? "up" : "down"}">
      <div class="stock-main">
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
      </div>
      <div class="stock-extra"></div>
    </div>`;
    enrichStockDetail(el, item);
  },
});

function marketCap(millions: number | null | undefined): string {
  if (millions == null) return "";
  if (millions >= 1_000_000) return `$${(millions / 1_000_000).toFixed(2)}T`;
  if (millions >= 1_000) return `$${(millions / 1_000).toFixed(0)}B`;
  return `$${millions.toFixed(0)}M`;
}

function newsAge(unix: number | null | undefined): string {
  if (!unix) return "";
  const minutes = Math.max(0, Math.round((Date.now() / 1000 - unix) / 60));
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.round(minutes / 60);
  return hours < 24 ? `${hours}h` : `${Math.round(hours / 24)}d`;
}

/** Fetch company profile / metrics / headlines and fill the right column. */
function enrichStockDetail(el: HTMLElement, item: any): void {
  fetch(`/api/markets/detail?symbol=${encodeURIComponent(item.symbol ?? "")}`)
    .then((r) => (r.ok ? r.json() : null))
    .then((d) => {
      if (!d || !el.isConnected) return;
      const extra = el.querySelector(".stock-extra");
      if (!extra) return;
      const rows: string[] = [];
      if (d.profile?.name) {
        const line = [d.profile.name, d.profile.industry, d.profile.exchange]
          .filter(Boolean)
          .join(" · ");
        rows.push(`<div class="stock-profile">${escapeHtml(line)}</div>`);
      }
      if (d.metrics) {
        const stats = [
          d.profile?.market_cap != null ? `Mkt cap ${marketCap(d.profile.market_cap)}` : "",
          d.metrics.pe != null ? `P/E ${Number(d.metrics.pe).toFixed(1)}` : "",
          d.metrics.beta != null ? `Beta ${Number(d.metrics.beta).toFixed(2)}` : "",
          d.metrics.div_yield ? `Div ${Number(d.metrics.div_yield).toFixed(2)}%` : "",
        ].filter(Boolean);
        if (stats.length) {
          rows.push(`<div class="stock-stats">${escapeHtml(stats.join(" · "))}</div>`);
        }
        if (d.recommendation) {
          const r = d.recommendation;
          const cls =
            r.label.includes("Buy") ? "rec-buy" : r.label === "Hold" ? "rec-hold" : "rec-sell";
          const seg = (n: number, c: string) =>
            n > 0 ? `<span class="${c}" style="flex:${n}"></span>` : "";
          rows.push(`<div class="stock-rec">
            <span class="stock-rec-label ${cls}">${escapeHtml(r.label)}</span>
            <span class="stock-rec-bar">
              ${seg(r.strong_buy + r.buy, "rec-buy")}${seg(r.hold, "rec-hold")}${seg(r.sell + r.strong_sell, "rec-sell")}
            </span>
            <span class="stock-rec-count">${r.total} analysts</span>
          </div>`);
        }
        if (d.earnings?.date) {
          const when = new Date(`${d.earnings.date}T12:00:00`);
          const day = when.toLocaleDateString([], {
            weekday: "short",
            month: "numeric",
            day: "numeric",
          });
          const hour =
            d.earnings.hour === "bmo"
              ? " · before open"
              : d.earnings.hour === "amc"
                ? " · after close"
                : "";
          const est =
            d.earnings.eps_estimate != null
              ? ` · est EPS ${Number(d.earnings.eps_estimate).toFixed(2)}`
              : "";
          const soon = when.getTime() - Date.now() < 7 * 86400e3;
          rows.push(
            `<div class="stock-earnings ${soon ? "soon" : ""}">Reports ${day}${hour}${est}</div>`,
          );
        }
        if (d.metrics.low52 != null && d.metrics.high52 != null) {
          rows.push(`<div class="quote-range stock-52w">
            <span class="range-label">52W ${formatPrice(d.metrics.low52)}</span>
            <span class="range-track"><span class="range-marker" style="left: ${Math.min(100, Math.max(0, ((item.price - d.metrics.low52) / (d.metrics.high52 - d.metrics.low52)) * 100)).toFixed(1)}%"></span></span>
            <span class="range-label">${formatPrice(d.metrics.high52)}</span>
          </div>`);
        }
      }
      if (d.news?.length) {
        rows.push(
          `<div class="stock-news">` +
            d.news
              .slice(0, 4)
              .map(
                (n: any) => `<div class="stock-news-item">
                  <div class="stock-news-headline">${escapeHtml(n.headline)}</div>
                  <div class="stock-news-meta">${escapeHtml(n.source ?? "")} · ${newsAge(n.datetime)} ago</div>
                </div>`,
              )
              .join("") +
            `</div>`,
        );
      }
      extra.innerHTML = rows.join("");
    })
    .catch(() => {});
}
