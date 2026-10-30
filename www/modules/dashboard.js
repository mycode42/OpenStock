import { monitoredStocks as monitoredStockList } from './dashboard-utils.js';
import { escapeHtml } from './escape.js';
import { money, formatLocalDateTime } from './formatting.js';

function formatMoney(value, currency = 'EUR') {
    if (value === null || value === undefined || value === '' || !Number.isFinite(Number(value))) return '—';
    try {
        return new Intl.NumberFormat(currency === 'USD' ? 'en-US' : 'de-DE', { style: 'currency', currency, minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(Number(value));
    } catch { return `${currency} ${money(value)}`; }
}

// Open-order status and cancel. New orders are placed from Configure, via the alert link.
function alpacaOrderControls(level) {
    const order = level.open_order;
    if (!order) return '';
    return `<div class="order-status">${escapeHtml(order.side)} ${escapeHtml(order.qty)} @ ${formatMoney(order.limit_price_usd, 'USD')} · ${escapeHtml(order.status)} <button type="button" class="btn btn-sm" data-alpaca-cancel-order="${escapeHtml(order.id)}">Cancel</button></div>`;
}

function priceChangePercent(currentPrice, buyInPrice) {
    if (currentPrice === null || currentPrice === undefined || currentPrice === '' || buyInPrice === null || buyInPrice === undefined || buyInPrice === '') return null;
    const current = Number(currentPrice);
    const buyIn = Number(buyInPrice);
    return Number.isFinite(current) && Number.isFinite(buyIn) && buyIn > 0
        ? (current / buyIn - 1) * 100
        : null;
}

function formatPriceChange(currentPrice, buyInPrice) {
    const change = priceChangePercent(currentPrice, buyInPrice);
    if (change === null) return { text: '—', className: '' };
    return { text: `${change >= 0 ? '+' : ''}${change.toFixed(2)}%`, className: change >= 0 ? 'up' : 'down' };
}

function liveQuoteSourceLabel(source) {
    if (source === 'demo') return 'Simulated market data';
    if (String(source || '').startsWith('alpaca_')) return `Alpaca ${String(source).split('_')[1]?.toUpperCase()} quotes`;
    return 'Alpaca market data';
}

function alertText(level) {
    if (level.trigger_mode === 'PERCENT') return `${level.side === 'BUY' ? 'DROP' : 'GAIN'} ${Number(level.change_pct).toFixed(2)}%`;
    return `${level.side} ${formatMoney(level.price_usd ?? level.price_eur, level.price_usd !== undefined ? 'USD' : 'EUR')}`;
}

export function createDashboard(context) {
    const { getStocks, request: af, charts, reloadCurrentPage } = context;
    const attachChartHovers = charts.attachChartHovers;
    const refreshOpenHistoryPanels = charts.refreshOpenHistoryPanels;
    const stockList = () => getStocks();
    let liveTimer = null;
    let liveRequestInFlight = false;
    let liveAbortController = null;
    const livePollMs = 1000;
    const chartRefreshMs = 60000;
    let chartRefreshTimer = null;
    let chartRefreshInFlight = false;
    function applyLivePrices(payload) {
        for (const quote of payload?.prices || []) {
            const card = document.querySelector(`.stock-card[data-stock-id="${quote.stock_id}"]`);
            if (!card) continue;
            const currency = quote.currency || card.dataset.currency || 'EUR';
            const priceValue = quote.price_usd ?? quote.price_eur;
            const price = card.querySelector('[data-live-price]');
            if (price) price.textContent = formatMoney(priceValue, currency);
            const buyInPrice = card.dataset.buyInPrice;
            const change = card.querySelector('[data-live-price-change]');
            if (change) {
                const formatted = formatPriceChange(priceValue, buyInPrice);
                change.textContent = formatted.text;
                change.className = `history-change ${formatted.className}`.trim();
            }
            const bid = card.querySelector('[data-live-bid]');
            if (bid) bid.textContent = formatMoney(quote.bid_usd ?? quote.bid_eur, currency);
            const ask = card.querySelector('[data-live-ask]');
            if (ask) ask.textContent = formatMoney(quote.ask_usd ?? quote.ask_eur, currency);
            const status = card.querySelector('[data-live-status]');
            if (status) status.textContent = `${liveQuoteSourceLabel(quote.source)} · ${quote.source === 'demo' ? 'demo' : 'live'}`;
        }
    }
    async function pollLivePrices() {
        if (liveRequestInFlight || document.hidden || (window.location.hash && window.location.hash !== '#/dashboard')) return;
        liveRequestInFlight = true;
        liveAbortController = new AbortController();
        try { applyLivePrices(await af('/live-prices', { signal: liveAbortController.signal })); }
        finally { liveAbortController = null; liveRequestInFlight = false; }
    }
    async function refreshOpenCharts() {
        if (chartRefreshInFlight || document.hidden || (window.location.hash && window.location.hash !== '#/dashboard')) return;
        chartRefreshInFlight = true;
        try { await refreshOpenHistoryPanels(document.getElementById('page-dashboard')); }
        finally { chartRefreshInFlight = false; }
    }
    function startLivePolling() {
        if (liveTimer) return;
        pollLivePrices();
        liveTimer = window.setInterval(pollLivePrices, livePollMs);
        chartRefreshTimer = window.setInterval(refreshOpenCharts, chartRefreshMs);
    }
    function stopLivePolling() {
        if (liveTimer) window.clearInterval(liveTimer);
        if (chartRefreshTimer) window.clearInterval(chartRefreshTimer);
        liveTimer = null;
        chartRefreshTimer = null;
        liveAbortController?.abort();
    }
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden && liveTimer) {
            pollLivePrices();
            refreshOpenHistoryPanels(document.getElementById('page-dashboard'), { includePreviouslyOpened: true });
        }
    });
function renderDashboard() {
    const container = document.getElementById('page-dashboard');
    // The Dashboard is the active monitoring view. Disabled stocks remain in
    // Configure, but are intentionally excluded from every dashboard metric
    // and card.
    const monitoredStocks = monitoredStockList(stockList());
    const currency = 'USD';
    const demoMode = monitoredStocks.some(stock => stock.market_data?.source === 'demo');
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
        const stockCurrency = stock.currency || currency;
        const levels = stock.alert_levels || [];
        const active = levels.filter(level => level.enabled && level.active && !level.retired);
        const retired = levels.filter(level => level.triggered || level.retired);
        const quantity = Number(position.quantity || 0);
        const bid = stock.broker_quote?.bid_usd ?? stock.broker_quote?.bid_eur ?? stock.current_bid_eur;
        const ask = stock.broker_quote?.ask_usd ?? stock.broker_quote?.ask_eur ?? stock.current_ask_eur;
        const currentPrice = stockCurrency === 'USD' ? stock.current_price_usd : stock.current_price_eur;
        const buyInPrice = Number(stockCurrency === 'USD' ? position.buy_in_price_usd : position.buy_in_price_eur);
        const priceChange = formatPriceChange(currentPrice, buyInPrice);
        const sellAlertNote = !position.owned && active.some(level => level.side === 'SELL')
            ? '<div class="position-summary">SELL alerts wait for an open Alpaca position.</div>'
            : '';
        const heldValue = position.owned
            ? `<div class="position-summary">Buy-in: <strong>${formatMoney(buyInPrice, stockCurrency)}</strong> <span class="history-change ${priceChange.className}" data-live-price-change>${priceChange.text}</span></div>`
            : '';
        return `<div class="stock-card ${position.owned ? '' : 'sold'}" data-stock-id="${escapeHtml(stock.id)}" data-broker="alpaca" data-currency="${escapeHtml(stockCurrency)}" data-buy-in-price="${position.owned && Number.isFinite(buyInPrice) ? escapeHtml(buyInPrice) : ''}" tabindex="0">
            <div class="stock-symbol">${escapeHtml(stock.symbol)} <span class="badge ${position.owned ? 'owned' : 'sold'}">${position.owned ? 'HELD' : 'NOT HELD'}</span> <span class="badge enabled">${escapeHtml(position.source)}</span></div>
            <div class="stock-name">${escapeHtml(stock.name || '')}</div>
            <div class="stock-price${stockCurrency === 'EUR' ? ' eu' : ''}" data-live-price>${formatMoney(currentPrice, stockCurrency)}</div>
            <div class="quote-sides" aria-label="Live bid and ask prices"><span>Bid <strong data-live-bid>${formatMoney(bid, stockCurrency)}</strong></span><span>Ask <strong data-live-ask>${formatMoney(ask, stockCurrency)}</strong></span></div>
            ${heldValue}
            ${sellAlertNote}
            <div class="level-summary"><strong>Active alerts</strong><div>${active.length ? active.map(level => `<a class="threshold ${level.side === 'BUY' ? 'buy' : 'sell'} alert-link" href="#/config?stock=${escapeHtml(stock.id)}&level=${escapeHtml(level.id)}" title="Open this alert in Configure">${escapeHtml(alertText(level))}</a>${alpacaOrderControls(level)}`).join('') : 'None'}</div><strong>Triggered/retired</strong><div>${retired.length ? retired.map(level => `<a class="threshold disabled alert-link" href="#/config?stock=${escapeHtml(stock.id)}&level=${escapeHtml(level.id)}" title="Open this alert in Configure to rearm it">${escapeHtml(alertText(level))} · ${level.retired ? 'retired' : 'triggered'}</a>`).join('') : 'None'}</div></div>
            <div class="stock-footer" data-live-status>${stock.last_price_update ? `Updated ${formatLocalDateTime(stock.last_price_update)}` : 'Never updated'} · ${escapeHtml(stock.market_data?.label || 'No quote')}</div>${stock.market_data?.source === 'demo' ? '<div class="position-summary">Sample alerts · read only in demo mode</div>' : '<div class="position-summary">Alpaca alert levels</div>'}<div class="history-popover" role="status" aria-live="polite"></div>
        </div>`;
    }).join('');
    const emptyState = 'No tickers are configured in the Alpaca USD watchlist.';
    container.innerHTML = `<header><h1>OpenStock</h1><p class="subtitle">Monitoring ${enabledCount} stocks · ${ownedCount} held · ${currency}${demoMode ? ' · Simulated data' : ''}</p></header><div id="ac"></div>${triggerCount ? `<div class="alert-box success">⚡ ${triggerCount} triggered/retired level(s)</div>` : ''}<div class="summary"><div class="summary-card"><div class="summary-label">Monitored</div><div class="summary-value">${enabledCount}/25</div></div><div class="summary-card"><div class="summary-label">Held</div><div class="summary-value">${ownedCount}</div></div><div class="summary-card"><div class="summary-label">Triggered levels</div><div class="summary-value">${triggerCount}</div></div></div><div class="last-update">Last: ${formatLocalDateTime(lastUpdate)} · Open a stock chart with mouse, keyboard focus, or a tap.</div>${monitoredStocks.length ? `<div class="stock-grid">${cards}</div>` : `<div class="card empty-state">${emptyState}</div>`}`;
    container.querySelectorAll('[data-alpaca-cancel-order]').forEach(button => button.addEventListener('click', async () => {
        button.disabled = true;
        const result = await af(`/alpaca/orders/${encodeURIComponent(button.dataset.alpacaCancelOrder)}/cancel`, { method: 'POST' });
        if (!result) { button.disabled = false; return; }
        await reloadCurrentPage?.();
    }));
    attachChartHovers(container);
    startLivePolling();
}
    return { renderDashboard, stopLivePolling };
}
