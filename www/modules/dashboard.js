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
        if (stock.owned) ownedCount++;
        enabledCount++;
        if (stock.trigger_state === 'BUY_TRIGGERED' || stock.trigger_state === 'SELL_TRIGGERED') triggerCount++;
    });
    const lastUpdate = monitoredStocks.reduce((latest, stock) => {
        if (!stock.last_price_update) return latest;
        return !latest || stock.last_price_update > latest ? stock.last_price_update : latest;
    }, null) || '—';

    container.innerHTML = `<header><h1>📊 OpenStock</h1><p class="subtitle">Monitoring ${enabledCount} stocks · ${ownedCount} owned</p></header><div id="ac"></div>${triggerCount ? `<div class="alert-box success">⚡ ${triggerCount} trigger(s) active</div>` : ''}<div class="summary"><div class="summary-card"><div class="summary-label">Monitored</div><div class="summary-value">${enabledCount}/25</div></div><div class="summary-card"><div class="summary-label">Owned</div><div class="summary-value">${ownedCount}</div></div><div class="summary-card"><div class="summary-label">Triggers</div><div class="summary-value">${triggerCount}</div></div></div><div class="last-update">Last: ${formatLocalDateTime(lastUpdate)} · Open a stock chart with mouse, keyboard focus, or a tap.</div>${monitoredStocks.length ? `<div class="stock-grid">${monitoredStocks.map(stock => `<div class="stock-card ${stock.owned ? '' : 'sold'} ${stock.trigger_state === 'BUY_TRIGGERED' ? 'trigger-buy' : ''} ${stock.trigger_state === 'SELL_TRIGGERED' ? 'trigger-sell' : ''}" data-stock-id="${escapeHtml(stock.id)}" tabindex="0"><div class="stock-symbol">${escapeHtml(stock.symbol)} <span class="badge ${stock.owned ? 'owned' : 'sold'}">${stock.owned ? 'OWNED' : 'SOLD'}</span> <span class="badge enabled">ON</span> ${stock.trigger_state === 'BUY_TRIGGERED' ? '<span class="badge buy-trigger">🔼 BUY</span>' : ''} ${stock.trigger_state === 'SELL_TRIGGERED' ? '<span class="badge sell-trigger">🔽 SELL</span>' : ''}</div><div class="stock-name">${escapeHtml(stock.name || '')}</div><div class="stock-price eu">€${money(stock.current_price_eur)}</div><div class="stock-price usd">$${money(stock.current_price_usd)}</div><div class="thresholds"><span class="threshold buy">Buy €${money(stock.buy_threshold_eur)}</span><span class="threshold sell">Sell €${money(stock.sell_threshold_eur)}</span></div><div class="stock-actions"><button class="btn btn-sm btn-secondary" data-stock-action="owned" data-stock-symbol="${escapeHtml(stock.symbol)}">${stock.owned ? 'Mark Sold' : 'Mark Owned'}</button><button class="btn btn-sm btn-secondary" data-stock-action="toggle" data-stock-symbol="${escapeHtml(stock.symbol)}">Disable</button><button class="btn btn-sm btn-danger" data-stock-action="remove" data-stock-symbol="${escapeHtml(stock.symbol)}">Remove</button></div><div class="stock-footer">${stock.last_price_update ? `Updated ${formatLocalDateTime(stock.last_price_update)}` : 'Never updated'}</div><div class="history-popover" role="status" aria-live="polite"></div></div>`).join('')}</div>` : '<div class="card empty-state">No stocks are enabled. Enable a stock in Configure to monitor it here.</div>'}<div style="margin-top:2rem"><button class="btn btn-primary" data-dashboard-action="config">+ Add Stock</button> <button class="btn btn-secondary" data-dashboard-action="refresh">Refresh Prices</button></div>`;
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
