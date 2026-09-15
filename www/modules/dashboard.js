import { monitoredStocks as monitoredStockList } from './dashboard-utils.js';
import { escapeHtml } from './escape.js';
import { money, formatLocalDateTime } from './formatting.js';

export function createDashboard(context) {
    const { getStocks, request: af, replaceStocks, showA, renderCurrentPage, getStockRevision, incrementStockRevision, nav, charts, actions } = context;
    const attachChartHovers = charts.attachChartHovers;
    const stockList = () => getStocks();
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
        const usdPrice = Number(stock.current_price_usd || 0);
        const heldValue = position.owned
            ? `<div class="position-summary">Position value: ${money(quantity)} × €${money(stock.current_price_eur)} = <strong>€${money(positionValue)}</strong></div>`
            : '';
        return `<div class="stock-card ${position.owned ? '' : 'sold'}" data-stock-id="${escapeHtml(stock.id)}" tabindex="0">
            <div class="stock-symbol">${escapeHtml(stock.symbol)} <span class="badge ${position.owned ? 'owned' : 'sold'}">${position.owned ? 'HELD' : 'NOT HELD'}</span> <span class="badge enabled">${escapeHtml(position.source)}</span></div>
            <div class="stock-name">${escapeHtml(stock.name || '')}</div>
            <div class="stock-price eu">€${money(stock.current_price_eur)} ${usdPrice > 0 ? `<span class="stock-price-usd">($${money(usdPrice)})</span>` : ''}</div>
            ${stock.broker_quote ? `<div class="stock-quote">Bid €${money(stock.broker_quote.bid_eur)} · Ask €${money(stock.broker_quote.ask_eur)}</div>` : ''}
            ${heldValue}
            <div class="level-summary"><strong>Active levels</strong><div>${active.length ? active.map(level => `<span class="threshold ${level.side === 'BUY' ? 'buy' : 'sell'}">${escapeHtml(level.side)} €${money(level.price_eur)}</span>`).join('') : 'None'}</div><strong>Triggered/retired</strong><div>${retired.length ? retired.map(level => `<span class="threshold disabled">${escapeHtml(level.side)} €${money(level.price_eur)} · ${level.retired ? 'retired' : 'triggered'}</span>`).join('') : 'None'}</div></div>
            <div class="stock-footer">${stock.last_price_update ? `Updated ${formatLocalDateTime(stock.last_price_update)}` : 'Never updated'}</div><div class="history-popover" role="status" aria-live="polite"></div>
        </div>`;
    }).join('');
    container.innerHTML = `<header><h1>📊 OpenStock</h1><p class="subtitle">Monitoring ${enabledCount} stocks · ${ownedCount} held</p></header><div id="ac"></div>${triggerCount ? `<div class="alert-box success">⚡ ${triggerCount} triggered/retired level(s)</div>` : ''}<div class="summary"><div class="summary-card"><div class="summary-label">Monitored</div><div class="summary-value">${enabledCount}/25</div></div><div class="summary-card"><div class="summary-label">Held</div><div class="summary-value">${ownedCount}</div></div><div class="summary-card"><div class="summary-label">Triggered levels</div><div class="summary-value">${triggerCount}</div></div></div><div class="last-update">Last: ${formatLocalDateTime(lastUpdate)} · Open a stock chart with mouse, keyboard focus, or a tap.</div>${monitoredStocks.length ? `<div class="stock-grid">${cards}</div>` : '<div class="card empty-state">No stocks are enabled. Enable a stock in Configure to monitor it here.</div>'}<div style="margin-top:2rem"><button class="btn btn-primary" data-dashboard-action="config">+ Add Stock</button> <button class="btn btn-secondary" data-dashboard-action="refresh">Refresh Prices</button></div>`;
    container.querySelectorAll('[data-stock-action]').forEach(button => button.addEventListener('click', () => actions[button.dataset.stockAction]?.(button.dataset.stockSymbol)));
    container.querySelector('[data-dashboard-action="config"]')?.addEventListener('click', () => nav('#/config'));
    container.querySelector('[data-dashboard-action="refresh"]')?.addEventListener('click', refreshPrices);
    attachChartHovers(container);
}

async function refreshPrices() {
    const button = document.querySelector('[data-dashboard-action="refresh"]');
    if (button) {
        button.disabled = true;
        button.textContent = 'Refreshing...';
    }
    const refreshRevision = incrementStockRevision();
    const result = await af('/refresh-prices', { method: 'POST' });
    if (!result) {
        if (button) {
            button.disabled = false;
            button.textContent = 'Refresh Prices';
        }
        showA('Failed to refresh prices', 'error');
        return;
    }

    if (getStockRevision() === refreshRevision && Array.isArray(result.stocks)) {
        replaceStocks(result.stocks);
    }
    renderCurrentPage();
    showA(`Refreshed ${result.updated} stock prices`, 'success');
}


    return { renderDashboard, refreshPrices };
}
