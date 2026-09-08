const API = './api';

let stocks = [];
let sMap = {};
let stockRevision = 0;
let pageLoadId = 0;
let editingStockSymbol = null;
const stockMutationTokens = new Map();
const historyCache = new Map();

function replaceStocks(nextStocks) {
    stocks = [...nextStocks].sort((a, b) => a.symbol.localeCompare(b.symbol));
    sMap = Object.fromEntries(stocks.map(stock => [stock.symbol, stock]));
}

function upsertStock(stock) {
    replaceStocks([...stocks.filter(item => item.id !== stock.id), stock]);
}

function removeStockFromState(id) {
    replaceStocks(stocks.filter(stock => stock.id !== id));
}

function beginStockMutation(id) {
    const token = (stockMutationTokens.get(id) || 0) + 1;
    stockMutationTokens.set(id, token);
    stockRevision++;
    return token;
}

function isCurrentStockMutation(id, token) {
    return stockMutationTokens.get(id) === token;
}

function pageFromHash(hash = window.location.hash) {
    const page = hash.replace('#/', '') || 'dashboard';
    return ['dashboard', 'config'].includes(page) ? page : 'dashboard';
}

function showPage(page) {
    document.querySelectorAll('nav a').forEach(link => {
        link.classList.toggle('active', link.dataset.page === page);
    });
    ['dashboard', 'config'].forEach(name => {
        document.getElementById(`page-${name}`).classList.toggle('hidden', name !== page);
    });
}

function nav(hash) {
    const page = pageFromHash(hash);
    const targetHash = page === 'dashboard' ? '' : `#/${page}`;
    if (window.location.hash !== targetHash) {
        window.location.hash = targetHash;
        return;
    }
    loadPage(page);
}

window.addEventListener('hashchange', () => loadPage(pageFromHash()));

async function af(path, options = {}) {
    try {
        const response = await fetch(API + path, {
            ...options,
            cache: 'no-store',
            headers: {
                Accept: 'application/json',
                ...(options.headers || {})
            }
        });
        const data = await response.json();
        if (!response.ok) {
            showA(data.error || 'Error', 'error');
            return null;
        }
        return data;
    } catch (error) {
        showA(`Network: ${error.message}`, 'error');
        return null;
    }
}

async function loadPage(page) {
    const loadId = ++pageLoadId;
    const startingStockRevision = stockRevision;
    showPage(page);
    document.getElementById(`page-${page}`).innerHTML = '<div class="loading">Loading...</div>';

    if (page === 'dashboard') {
        const dashboard = await af('/dashboard');
        if (loadId !== pageLoadId || pageFromHash() !== page) return;
        if (dashboard && stockRevision === startingStockRevision) replaceStocks(dashboard.stocks);
        renderDashboard();
        return;
    }

    const freshStocks = await af('/stocks');
    if (loadId !== pageLoadId || pageFromHash() !== page) return;
    if (freshStocks && stockRevision === startingStockRevision) replaceStocks(freshStocks);
    renderConfig();
}

function renderCurrentPage() {
    const page = pageFromHash();
    showPage(page);
    if (page === 'dashboard') renderDashboard();
    else renderConfig();
}

function showA(message, type = 'i', containerId = 'ac') {
    const container = document.getElementById(containerId);
    if (!container) return;
    const alert = document.createElement('div');
    alert.className = `alert-box ${type}`;
    alert.textContent = message;
    container.appendChild(alert);
    setTimeout(() => alert.remove(), 5000);
}

function dt(id) {
    document.getElementById(id)?.classList.toggle('open');
}

function di(id, items, selectedIndex, labelFn, valueFn, onChange) {
    const trigger = document.getElementById(`${id}-trigger`);
    const panel = document.getElementById(`${id}-panel`);
    const input = document.getElementById(id);
    if (!trigger || !panel || !input || items.length === 0) return;

    const safeIndex = Math.max(0, Math.min(selectedIndex, items.length - 1));
    trigger.textContent = labelFn(items[safeIndex], safeIndex);
    input.value = valueFn(items[safeIndex], safeIndex);
    panel.innerHTML = items.map((item, index) =>
        `<div class='dropdown-item ${index === safeIndex ? 'selected' : ''}' data-k='${index}'><span class='check'>✓</span>${labelFn(item, index)}</div>`
    ).join('');

    panel.querySelectorAll('.dropdown-item').forEach(item => item.addEventListener('click', () => {
        const index = Number(item.dataset.k);
        panel.querySelectorAll('.dropdown-item').forEach((option, optionIndex) => {
            option.classList.toggle('selected', optionIndex === index);
        });
        trigger.textContent = labelFn(items[index], index);
        input.value = valueFn(items[index], index);
        panel.parentElement.classList.remove('open');
        onChange?.(items[index], index);
    }));
}

document.addEventListener('click', event => {
    if (!event.target.closest('.dropdown')) {
        document.querySelectorAll('.dropdown.open').forEach(dropdown => dropdown.classList.remove('open'));
    }
});

function money(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number.toFixed(2) : '—';
}
function parseOpenStockDate(value) {
    if (!value) return null;
    const text = String(value).trim();
    if (!text) return null;

    // SQLite returns `YYYY-MM-DD HH:MM:SS`; convert it to explicit UTC ISO first.
    const sqliteMatch = text.match(/^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})(?:\.\d+)?$/);
    if (sqliteMatch) {
        return new Date(`${sqliteMatch[1]}T${sqliteMatch[2]}Z`);
    }

    const date = new Date(text);
    return Number.isNaN(date.getTime()) ? null : date;
}

function formatLocalDate(value) {
    const date = parseOpenStockDate(value);
    if (!date) return '—';
    return date.toLocaleString('en-GB', {
        timeZone: 'Europe/Berlin',
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        timeZoneName: 'short',
        hour12: false
    });
}

function formatLocalDateTime(value) {
    const date = parseOpenStockDate(value);
    if (!date) return 'Never updated';
    return date.toLocaleString('en-GB', {
        timeZone: 'Europe/Berlin',
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        timeZoneName: 'short',
        hour12: false
    });
}

function formatChartDate(value) {
    const date = parseOpenStockDate(value);
    if (!date) return '—';
    return date.toLocaleDateString('en-GB', {
        timeZone: 'Europe/Berlin', month: 'short', day: 'numeric'
    });
}

function chartRangeLabel(range) {
    return { '1d': '1-day', '1mo': '1-month', '1y': '1-year', '5y': '5-year' }[range] || range;
}

function chartRangeControls(activeRange) {
    return `<div class="history-ranges" role="group" aria-label="Chart range">${['1d', '1mo', '1y', '5y'].map(range => `<button class="history-range ${range === activeRange ? 'active' : ''}" type="button" data-history-range="${range}">${range === '1mo' ? '1M' : range.toUpperCase()}</button>`).join('')}</div>`;
}

function renderHistoryChart(points, range) {
    const clean = points.filter(point => Number.isFinite(Number(point.price_usd)) && parseOpenStockDate(point.recorded_at));
    if (clean.length < 2) return '<div class="history-empty">Collecting local price history…</div>';

    const values = clean.map(point => Number(point.price_usd));
    const min = Math.min(...values);
    const max = Math.max(...values);
    const padding = Math.max((max - min) * 0.08, max * 0.002, 0.01);
    const low = min - padding;
    const high = max + padding;
    const width = 264;
    const height = 82;
    const verticalRange = high - low || 1;
    const polyline = clean.map((point, index) => {
        const x = (index / (clean.length - 1)) * width;
        const y = height - ((Number(point.price_usd) - low) / verticalRange) * height;
        return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');
    const first = clean[0];
    const last = clean[clean.length - 1];
    const change = ((Number(last.price_usd) / Number(first.price_usd)) - 1) * 100;
    const changeClass = change >= 0 ? 'up' : 'down';
    const lastY = height - ((Number(last.price_usd) - low) / verticalRange) * height;

    return `<div class="history-meta"><span>$${money(min)} – $${money(max)}</span><span class="history-change ${changeClass}">${change >= 0 ? '+' : ''}${change.toFixed(2)}%</span></div><svg class="history-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="${chartRangeLabel(range)} USD price chart from ${formatChartDate(first.recorded_at)} to ${formatChartDate(last.recorded_at)}"><path class="history-grid" d="M0 20H${width} M0 41H${width} M0 62H${width}"/><polyline class="history-line ${changeClass}" points="${polyline}"/><circle class="history-dot ${changeClass}" cx="${width}" cy="${lastY.toFixed(1)}" r="3"/></svg><div class="history-dates"><span>${formatChartDate(first.recorded_at)}</span><span>${formatChartDate(last.recorded_at)}</span></div>`;
}

function renderHistoryPanel(panel, history, range) {
    panel.innerHTML = `<div class="history-header"><div class="history-title">${chartRangeLabel(range)} · USD <span>${history.points.length} local points</span></div>${chartRangeControls(range)}</div>${renderHistoryChart(history.points, range)}`;
}

async function loadCardHistory(card, range = '1mo') {
    const stockId = Number(card.dataset.stockId);
    const panel = card.querySelector('.history-popover');
    if (!Number.isInteger(stockId) || !panel) return;
    const key = `${stockId}:${range}`;
    panel.dataset.requestedRange = range;
    panel.innerHTML = `<div class="history-header"><div class="history-title">${chartRangeLabel(range)} · USD</div>${chartRangeControls(range)}</div><div class="history-loading">Loading local history…</div>`;
    let history = historyCache.get(key);
    if (!history) {
        history = await af(`/stocks/${stockId}/history?range=${range}`);
        if (history) historyCache.set(key, history);
    }
    if (panel.dataset.requestedRange !== range) return;
    if (!history) {
        panel.innerHTML = '<div class="history-empty">History is unavailable.</div>';
        return;
    }
    renderHistoryPanel(panel, history, range);
}

function attachChartHovers(container) {
    if (!window.matchMedia('(hover: hover) and (pointer: fine)').matches) return;
    container.querySelectorAll('.stock-card[data-stock-id]').forEach(card => {
        card.addEventListener('mouseenter', () => loadCardHistory(card, '1mo'), { once: true });
        card.addEventListener('focusin', () => loadCardHistory(card, '1mo'), { once: true });
        card.querySelector('.history-popover').addEventListener('click', event => {
            const button = event.target.closest('[data-history-range]');
            if (!button) return;
            event.preventDefault();
            event.stopPropagation();
            loadCardHistory(card, button.dataset.historyRange);
        });
    });
}

function renderDashboard() {
    const container = document.getElementById('page-dashboard');
    let ownedCount = 0;
    let enabledCount = 0;
    let triggerCount = 0;
    stocks.forEach(stock => {
        if (stock.owned) ownedCount++;
        if (stock.enabled) enabledCount++;
        if (stock.trigger_state === 'BUY_TRIGGERED' || stock.trigger_state === 'SELL_TRIGGERED') triggerCount++;
    });
    const lastUpdate = stocks.reduce((latest, stock) => {
        if (!stock.last_price_update) return latest;
        return !latest || stock.last_price_update > latest ? stock.last_price_update : latest;
    }, null) || '—';

    container.innerHTML = `<header><h1>📊 OpenStock</h1><p class="subtitle">Monitoring ${enabledCount} stocks · ${ownedCount} owned</p></header><div id="ac"></div>${triggerCount ? `<div class="alert-box success">⚡ ${triggerCount} trigger(s) active</div>` : ''}<div class="summary"><div class="summary-card"><div class="summary-label">Monitored</div><div class="summary-value">${enabledCount}/25</div></div><div class="summary-card"><div class="summary-label">Owned</div><div class="summary-value">${ownedCount}</div></div><div class="summary-card"><div class="summary-label">Triggers</div><div class="summary-value">${triggerCount}</div></div></div><div class="last-update">Last: ${formatLocalDateTime(lastUpdate)} · Hover a stock on desktop to view its local USD chart.</div><div class="stock-grid">${stocks.map(stock => `<div class="stock-card ${stock.enabled ? '' : 'disabled'} ${stock.owned ? '' : 'sold'} ${stock.trigger_state === 'BUY_TRIGGERED' ? 'trigger-buy' : ''} ${stock.trigger_state === 'SELL_TRIGGERED' ? 'trigger-sell' : ''}" data-stock-id="${stock.id}" tabindex="0"><div class="stock-symbol">${stock.symbol} <span class="badge ${stock.owned ? 'owned' : 'sold'}">${stock.owned ? 'OWNED' : 'SOLD'}</span> <span class="badge ${stock.enabled ? 'enabled' : 'disabled'}">${stock.enabled ? 'ON' : 'OFF'}</span> ${stock.trigger_state === 'BUY_TRIGGERED' ? '<span class="badge buy-trigger">🔼 BUY</span>' : ''} ${stock.trigger_state === 'SELL_TRIGGERED' ? '<span class="badge sell-trigger">🔽 SELL</span>' : ''}</div><div class="stock-name">${stock.name || ''}</div><div class="stock-price eu">€${money(stock.current_price_eur)}</div><div class="stock-price usd">$${money(stock.current_price_usd)}</div><div class="thresholds"><span class="threshold buy">Buy €${money(stock.buy_threshold_eur)}</span><span class="threshold sell">Sell €${money(stock.sell_threshold_eur)}</span></div><div class="stock-actions"><button class="btn btn-sm btn-secondary" onclick="tos('${stock.symbol}')">${stock.owned ? 'Mark Sold' : 'Mark Owned'}</button><button class="btn btn-sm btn-secondary" onclick="ts('${stock.symbol}')">${stock.enabled ? 'Disable' : 'Enable'}</button><button class="btn btn-sm btn-danger" onclick="rs('${stock.symbol}')">Remove</button></div><div class="stock-footer">${stock.last_price_update ? `Updated ${formatLocalDateTime(stock.last_price_update)}` : 'Never updated'}</div><div class="history-popover" role="status" aria-live="polite"></div></div>`).join('')}</div><div style="margin-top:2rem"><button class="btn btn-primary" onclick="nav('#/config')">+ Add Stock</button> <button class="btn btn-secondary" onclick="refreshPrices()">Refresh Prices</button></div>`;
    attachChartHovers(container);
}

async function refreshPrices() {
    const button = document.querySelector('button[onclick="refreshPrices()"]');
    if (button) {
        button.disabled = true;
        button.textContent = 'Refreshing...';
    }
    const refreshRevision = ++stockRevision;
    const result = await af('/refresh-prices', { method: 'POST' });
    if (!result) {
        if (button) {
            button.disabled = false;
            button.textContent = 'Refresh Prices';
        }
        showA('Failed to refresh prices', 'error');
        return;
    }

    if (stockRevision === refreshRevision && Array.isArray(result.stocks)) {
        replaceStocks(result.stocks);
    }
    renderCurrentPage();
    showA(`Refreshed ${result.updated} stock prices`, 'success');
}

function renderConfig() {
    const container = document.getElementById('page-config');
    container.innerHTML = `<header><h1>⚙️ Stock Configuration</h1><p class="subtitle">Manage monitored stocks</p></header><div id="ac"></div><div class="card"><div class="section-header"><div class="section-title">${stocks.length}/25</div></div><div style="overflow-x:auto"><table class="table"><thead><tr><th>Symbol</th><th>Name</th><th>€ Price</th><th>Buy</th><th>Sell</th><th>Status</th><th>On</th><th></th></tr></thead><tbody>${stocks.map(stock => `<tr><td><strong>${stock.symbol}</strong></td><td>${stock.name || '—'}</td><td>€${money(stock.current_price_eur)}</td><td>€${money(stock.buy_threshold_eur)}</td><td>€${money(stock.sell_threshold_eur)}</td><td><span class="badge ${stock.owned ? 'owned' : 'sold'}">${stock.owned ? 'OWNED' : 'SOLD'}</span></td><td><span class="badge ${stock.enabled ? 'enabled' : 'disabled'}">${stock.enabled ? 'ON' : 'OFF'}</span></td><td><button class="btn btn-sm btn-secondary" onclick="es('${stock.symbol}')">Edit</button> <button class="btn btn-sm btn-secondary" onclick="tos('${stock.symbol}')">${stock.owned ? 'Mark Sold' : 'Mark Owned'}</button> <button class="btn btn-sm btn-secondary" onclick="ts('${stock.symbol}')">${stock.enabled ? 'Disable' : 'Enable'}</button> <button class="btn btn-sm btn-danger" onclick="rs('${stock.symbol}')">Remove</button></td></tr>`).join('')}</tbody></table></div><div style="margin-top:1rem"><h3 id="stock-form-title">Add Stock</h3><div class="form-row"><div class="form-group"><label>Symbol</label><input id="as-sym" placeholder="AAPL"></div><div class="form-group"><label>Name</label><input id="as-nm" placeholder="Apple Inc."></div></div><div class="form-row"><div class="form-group"><label>Buy Target €</label><input id="as-buy" type="number" step=".01" min="0"></div><div class="form-group"><label>Sell Target €</label><input id="as-sell" type="number" step=".01" min="0"></div></div><div class="btn-group"><button id="stock-form-submit" class="btn btn-primary" onclick="asm()">Add</button><button id="stock-form-cancel" class="btn btn-secondary hidden" onclick="cancelStockEdit()">Cancel</button></div></div><div id="as-result" style="margin-top:.75rem"></div></div>`;
    populateStockEditor();
}

function populateStockEditor() {
    const stock = editingStockSymbol ? sMap[editingStockSymbol] : null;
    if (!stock) {
        editingStockSymbol = null;
        return;
    }
    document.getElementById('stock-form-title').textContent = `Edit ${stock.symbol}`;
    document.getElementById('as-sym').value = stock.symbol;
    document.getElementById('as-sym').disabled = true;
    document.getElementById('as-nm').value = stock.name || '';
    document.getElementById('as-buy').value = stock.buy_threshold_eur;
    document.getElementById('as-sell').value = stock.sell_threshold_eur;
    const submit = document.getElementById('stock-form-submit');
    submit.textContent = 'Save';
    submit.onclick = () => esu(stock.symbol);
    document.getElementById('stock-form-cancel').classList.remove('hidden');
}

function cancelStockEdit() {
    editingStockSymbol = null;
    renderConfig();
}

async function asm() {
    const symbolInput = document.getElementById('as-sym');
    if (!symbolInput) {
        nav('#/config');
        return;
    }
    const symbol = symbolInput.value.trim().toUpperCase();
    const name = document.getElementById('as-nm').value.trim();
    const buyThreshold = parseFloat(document.getElementById('as-buy').value);
    const sellThreshold = parseFloat(document.getElementById('as-sell').value);
    if (!symbol || !name) {
        showA('Symbol & name required', 'error');
        return;
    }
    if (isNaN(buyThreshold) || isNaN(sellThreshold) || buyThreshold >= sellThreshold) {
        showA('Invalid thresholds (buy < sell)', 'error');
        return;
    }
    stockRevision++;
    const result = await af('/stocks', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            symbol,
            name,
            buy_threshold_eur: buyThreshold,
            sell_threshold_eur: sellThreshold
        })
    });
    if (result?.stock) {
        upsertStock(result.stock);
        renderConfig();
        showA(`Added ${symbol}`, 'success');
    }
}

function es(symbol) {
    if (!sMap[symbol]) return;
    editingStockSymbol = symbol;
    if (pageFromHash() === 'config') renderConfig();
    else nav('#/config');
}

async function esu(symbol) {
    const stock = sMap[symbol];
    if (!stock) return;
    const name = document.getElementById('as-nm').value.trim();
    const buyThreshold = parseFloat(document.getElementById('as-buy').value);
    const sellThreshold = parseFloat(document.getElementById('as-sell').value);
    if (!name || isNaN(buyThreshold) || isNaN(sellThreshold) || buyThreshold >= sellThreshold) {
        showA('Invalid name or thresholds (buy < sell)', 'error');
        return;
    }
    const token = beginStockMutation(stock.id);
    const result = await af(`/stocks/${stock.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            name,
            buy_threshold_eur: buyThreshold,
            sell_threshold_eur: sellThreshold
        })
    });
    if (result?.stock && isCurrentStockMutation(stock.id, token)) {
        editingStockSymbol = null;
        upsertStock(result.stock);
        renderCurrentPage();
        showA(`Updated ${symbol}`, 'success');
    }
}

async function ts(symbol) {
    const stock = sMap[symbol];
    if (!stock) return;
    const token = beginStockMutation(stock.id);
    const result = await af(`/stocks/${stock.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: stock.enabled ? 0 : 1 })
    });
    if (result?.stock && isCurrentStockMutation(stock.id, token)) {
        upsertStock(result.stock);
        renderCurrentPage();
        showA(`${symbol} ${result.stock.enabled ? 'enabled' : 'disabled'}`, 'success');
    }
}

async function tos(symbol) {
    const stock = sMap[symbol];
    if (!stock) return;
    const token = beginStockMutation(stock.id);
    const result = await af(`/stocks/${stock.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ owned: stock.owned ? 0 : 1 })
    });
    if (result?.stock && isCurrentStockMutation(stock.id, token)) {
        upsertStock(result.stock);
        renderCurrentPage();
        showA(`${symbol} marked ${result.stock.owned ? 'owned' : 'sold'}`, 'success');
    }
}

async function rs(symbol) {
    const stock = sMap[symbol];
    if (!stock || !confirm(`Remove ${symbol}?`)) return;
    const token = beginStockMutation(stock.id);
    const result = await af(`/stocks/${stock.id}`, { method: 'DELETE' });
    if (result && isCurrentStockMutation(stock.id, token)) {
        if (editingStockSymbol === symbol) editingStockSymbol = null;
        removeStockFromState(stock.id);
        renderCurrentPage();
        showA(`Removed ${symbol}`, 'success');
    }
}

loadPage(pageFromHash());
