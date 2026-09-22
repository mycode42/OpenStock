import { monitoredStocks as monitoredStockList } from './dashboard-utils.js';
import { escapeHtml } from './escape.js';
import { money, formatLocalDateTime } from './formatting.js';

function quoteMoney(value) {
    if (value === null || value === undefined || value === '') return '—';
    return Number.isFinite(Number(value)) ? `€${money(value)}` : '—';
}

export function createDashboard(context) {
    const { getStocks, request: af, charts, actions } = context;
    const attachChartHovers = charts.attachChartHovers;
    const stockList = () => getStocks();
    let liveTimer = null;
    let liveRequestInFlight = false;
    let liveAbortController = null;
    const livePollMs = 1000;
    function applyLivePrices(payload) {
        for (const quote of payload?.prices || []) {
            const card = document.querySelector(`.stock-card[data-stock-id="${quote.stock_id}"]`);
            if (!card) continue;
            const price = card.querySelector('[data-live-price]');
            if (price) price.textContent = `€${money(quote.price_eur)}`;
            const bid = card.querySelector('[data-live-bid]');
            if (bid) bid.textContent = quoteMoney(quote.bid_eur);
            const ask = card.querySelector('[data-live-ask]');
            if (ask) ask.textContent = quoteMoney(quote.ask_eur);
            const status = card.querySelector('[data-live-status]');
            if (status) status.textContent = `${quote.source === 'pytr_rates' ? 'Trade Republic pytr fallback' : quote.source === 'trade_republic_order_price' ? 'Trade Republic sidecar' : 'Yahoo Finance fallback'} · live`;
        }
    }
    async function pollLivePrices() {
        if (liveRequestInFlight || document.hidden || (window.location.hash && window.location.hash !== '#/dashboard')) return;
        liveRequestInFlight = true;
        liveAbortController = new AbortController();
        try { applyLivePrices(await af('/live-prices', { signal: liveAbortController.signal })); }
        finally { liveAbortController = null; liveRequestInFlight = false; }
    }
    function startLivePolling() {
        if (liveTimer) return;
        pollLivePrices();
        liveTimer = window.setInterval(pollLivePrices, livePollMs);
    }
    function stopLivePolling() {
        if (liveTimer) window.clearInterval(liveTimer);
        liveTimer = null;
        liveAbortController?.abort();
    }
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden && liveTimer) pollLivePrices();
    });
function renderDashboard() {
    const container = document.getElementById('page-dashboard');
    // The Dashboard is the active monitoring view. Disabled stocks remain in
    // Configure, but are intentionally excluded from every dashboard metric
    // and card.
    const monitoredStocks = monitoredStockList(stockList());
    let ownedCount = 0;
    let enabledCount = 0;
    let triggerCount = 0;
    monitoredStocks.forEach(stock => {
        if (stock.position?.owned) ownedCount++;
        enabledCount++;
        triggerCount += (stock.alert_levels || []).filter(level => level.triggered || level.retired).length;
    });
    const lastUpdate = monitoredStocks.reduce((latest, stock) => {
        if (!stock.last_price_update) return latest;
        return !latest || stock.last_price_update > latest ? stock.last_price_update : latest;
    }, null) || '—';

    const cards = monitoredStocks.map(stock => {
        const position = stock.position || { owned: false, quantity: 0, source: 'journal' };
        const levels = stock.alert_levels || [];
        const active = levels.filter(level => level.enabled && level.active && !level.retired);
        const retired = levels.filter(level => level.triggered || level.retired);
        const quantity = Number(position.quantity || 0);
        const positionValue = quantity * Number(stock.current_price_eur || 0);
        const bid = stock.broker_quote?.bid_eur ?? stock.current_bid_eur;
        const ask = stock.broker_quote?.ask_eur ?? stock.current_ask_eur;
        const heldValue = position.owned
            ? `<div class="position-summary">Position value: ${money(quantity)} × €${money(stock.current_price_eur)} = <strong>€${money(positionValue)}</strong></div>`
            : '';
        return `<div class="stock-card ${position.owned ? '' : 'sold'}" data-stock-id="${escapeHtml(stock.id)}" tabindex="0">
            <div class="stock-symbol">${escapeHtml(stock.symbol)} <span class="badge ${position.owned ? 'owned' : 'sold'}">${position.owned ? 'HELD' : 'NOT HELD'}</span> <span class="badge enabled">${escapeHtml(position.source)}</span></div>
            <div class="stock-name">${escapeHtml(stock.name || '')}</div>
            <div class="stock-price eu" data-live-price>€${money(stock.current_price_eur)}</div>
            <div class="quote-sides" aria-label="Live bid and ask prices"><span>Bid <strong data-live-bid>${quoteMoney(bid)}</strong></span><span>Ask <strong data-live-ask>${quoteMoney(ask)}</strong></span></div>
            ${heldValue}
            <div class="level-summary"><strong>Active levels</strong><div>${active.length ? active.map(level => `<span class="threshold ${level.side === 'BUY' ? 'buy' : 'sell'}">${escapeHtml(level.side)} €${money(level.price_eur)}</span>`).join('') : 'None'}</div><strong>Triggered/retired</strong><div>${retired.length ? retired.map(level => `<span class="threshold disabled">${escapeHtml(level.side)} €${money(level.price_eur)} · ${level.retired ? 'retired' : 'triggered'}</span>`).join('') : 'None'}</div></div>
            <div class="stock-footer" data-live-status>${stock.last_price_update ? `Updated ${formatLocalDateTime(stock.last_price_update)}` : 'Never updated'} · ${escapeHtml(stock.market_data?.label || 'No quote')}</div><div class="history-popover" role="status" aria-live="polite"></div>
        </div>`;
    }).join('');
    container.innerHTML = `<header><h1>📊 OpenStock</h1><p class="subtitle">Monitoring ${enabledCount} stocks · ${ownedCount} held</p></header><div id="ac"></div>${triggerCount ? `<div class="alert-box success">⚡ ${triggerCount} triggered/retired level(s)</div>` : ''}<div class="summary"><div class="summary-card"><div class="summary-label">Monitored</div><div class="summary-value">${enabledCount}/25</div></div><div class="summary-card"><div class="summary-label">Held</div><div class="summary-value">${ownedCount}</div></div><div class="summary-card"><div class="summary-label">Triggered levels</div><div class="summary-value">${triggerCount}</div></div></div><div class="last-update">Last: ${formatLocalDateTime(lastUpdate)} · Open a stock chart with mouse, keyboard focus, or a tap.</div>${monitoredStocks.length ? `<div class="stock-grid">${cards}</div>` : '<div class="card empty-state">No stocks are enabled. Enable a stock in Configure to monitor it here.</div>'}`;
    container.querySelectorAll('[data-stock-action]').forEach(button => button.addEventListener('click', () => actions[button.dataset.stockAction]?.(button.dataset.stockSymbol)));
    attachChartHovers(container);
    startLivePolling();
}
    return { renderDashboard, stopLivePolling };
}
