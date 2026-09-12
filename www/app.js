const API = './api';

let stocks = [];
let sMap = {};
let tradingAnalytics = null;
let trades = [];
let capitalEvents = [];
let editingTradeId = null;
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
    return ['dashboard', 'config', 'journal'].includes(page) ? page : 'dashboard';
}

function showPage(page) {
    document.querySelectorAll('nav a').forEach(link => {
        link.classList.toggle('active', link.dataset.page === page);
    });
    ['dashboard', 'config', 'journal'].forEach(name => {
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

    if (page === 'journal') {
        const [freshStocks, freshTrades, freshEvents, freshAnalytics] = await Promise.all([
            af('/stocks?includeArchived=1'), af('/trades'), af('/capital-events'), af('/analytics/dashboard')
        ]);
        if (loadId !== pageLoadId || pageFromHash() !== page) return;
        if (freshStocks && stockRevision === startingStockRevision) replaceStocks(freshStocks);
        if (freshTrades) trades = freshTrades;
        if (freshEvents) capitalEvents = freshEvents;
        if (freshAnalytics) tradingAnalytics = freshAnalytics;
        renderJournal();
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
    else if (page === 'journal') renderJournal();
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

function interactiveChartSvg({ className, width, height, grid, lineClass = '', linePoints, hoverPoints, ariaLabel }) {
    const encodedPoints = journalEscape(JSON.stringify(hoverPoints));
    return `<svg class="${className} interactive-line-chart" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" tabindex="0" role="img" aria-label="${journalEscape(ariaLabel)}" data-chart-label="${journalEscape(ariaLabel)}" data-chart-points="${encodedPoints}">${grid}<polyline class="${lineClass}" points="${linePoints}"/><g class="chart-hover" hidden><line class="chart-hover-guide" y1="0" y2="${height}"/><circle class="chart-hover-dot" r="4"/><text class="chart-hover-text"><tspan class="chart-hover-primary"/><tspan class="chart-hover-secondary"/><tspan class="chart-hover-tertiary"/></text></g><rect class="chart-hit-area" x="0" y="0" width="${width}" height="${height}"/></svg>`;
}

function attachInteractiveLineCharts(root) {
    root.querySelectorAll('.interactive-line-chart').forEach(chart => {
        if (chart.dataset.chartBound === '1') return;
        chart.dataset.chartBound = '1';
        let points;
        try { points = JSON.parse(chart.dataset.chartPoints || '[]'); } catch { points = []; }
        if (!points.length) return;
        const hover = chart.querySelector('.chart-hover');
        const guide = chart.querySelector('.chart-hover-guide');
        const dot = chart.querySelector('.chart-hover-dot');
        const text = chart.querySelector('.chart-hover-text');
        const primary = chart.querySelector('.chart-hover-primary');
        const secondary = chart.querySelector('.chart-hover-secondary');
        const tertiary = chart.querySelector('.chart-hover-tertiary');
        const hitArea = chart.querySelector('.chart-hit-area');
        let activeIndex = points.length - 1;
        let pinned = false;

        const show = index => {
            activeIndex = Math.max(0, Math.min(points.length - 1, index));
            const point = points[activeIndex];
            const width = chart.viewBox.baseVal.width;
            const height = chart.viewBox.baseVal.height;
            const bounds = chart.getBoundingClientRect();
            const inverseScaleX = bounds.width ? width / bounds.width : 1;
            const inverseScaleY = bounds.height ? height / bounds.height : 1;
            const compensateStretch = (x, y) => `translate(${x} ${y}) scale(${inverseScaleX} ${inverseScaleY}) translate(${-x} ${-y})`;
            guide.setAttribute('x1', point.x); guide.setAttribute('x2', point.x);
            dot.setAttribute('cx', point.x); dot.setAttribute('cy', point.y);
            dot.setAttribute('transform', compensateStretch(point.x, point.y));
            const anchor = point.x < width * 0.22 ? 'start' : point.x > width * 0.78 ? 'end' : 'middle';
            const lineGap = point.lineGap || 9;
            const lineCount = point.tertiary ? 3 : 2;
            const blockHeight = (lineCount - 1) * lineGap;
            const labelY = point.y > blockHeight + 18 ? point.y - blockHeight - 10 : point.y + 16;
            text.setAttribute('x', point.x); text.setAttribute('y', labelY); text.setAttribute('text-anchor', anchor);
            text.setAttribute('transform', compensateStretch(point.x, labelY));
            primary.setAttribute('x', point.x);
            secondary.setAttribute('x', point.x); secondary.setAttribute('dy', lineGap);
            tertiary.setAttribute('x', point.x); tertiary.setAttribute('dy', lineGap);
            primary.textContent = point.primary;
            secondary.textContent = point.secondary;
            tertiary.textContent = point.tertiary || '';
            tertiary.toggleAttribute('hidden', !point.tertiary);
            hover.removeAttribute('hidden');
            chart.setAttribute('aria-label', `${chart.dataset.chartLabel}. ${point.primary}. ${point.secondary}${point.tertiary ? `. ${point.tertiary}` : ''}`);
        };
        const hide = () => { if (!pinned) { hover.setAttribute('hidden', ''); chart.setAttribute('aria-label', chart.dataset.chartLabel); } };
        const nearest = clientX => {
            const bounds = chart.getBoundingClientRect();
            const x = (clientX - bounds.left) / bounds.width * chart.viewBox.baseVal.width;
            let best = 0;
            for (let index = 1; index < points.length; index += 1) if (Math.abs(points[index].x - x) < Math.abs(points[best].x - x)) best = index;
            return best;
        };

        hitArea.addEventListener('pointermove', event => show(nearest(event.clientX)));
        hitArea.addEventListener('pointerleave', hide);
        hitArea.addEventListener('pointerdown', event => { pinned = true; chart.focus(); show(nearest(event.clientX)); });
        chart.addEventListener('focus', () => show(activeIndex));
        chart.addEventListener('blur', () => { pinned = false; hide(); });
        chart.addEventListener('keydown', event => {
            if (event.key === 'ArrowLeft') activeIndex -= 1;
            else if (event.key === 'ArrowRight') activeIndex += 1;
            else if (event.key === 'Home') activeIndex = 0;
            else if (event.key === 'End') activeIndex = points.length - 1;
            else if (event.key === 'Escape') { pinned = false; hide(); return; }
            else return;
            event.preventDefault(); pinned = true; show(activeIndex);
        });
    });
}

function renderHistoryChart(points, range) {
    const clean = points.filter(point => Number.isFinite(Number(point.price_usd)) && parseOpenStockDate(point.recorded_at));
    if (clean.length < 2) return '<div class="history-empty">Collecting local price history…</div>';
    const values = clean.map(point => Number(point.price_usd));
    const min = Math.min(...values), max = Math.max(...values);
    const padding = Math.max((max - min) * 0.08, max * 0.002, 0.01);
    const low = min - padding, high = max + padding, width = 264, height = 82;
    const times = clean.map(point => parseOpenStockDate(point.recorded_at).getTime());
    const firstTime = times[0], timeRange = times[times.length - 1] - firstTime || 1;
    const coordinates = clean.map((point, index) => ({ point, x: (times[index] - firstTime) / timeRange * width, y: height - ((Number(point.price_usd) - low) / (high - low || 1) * height) }));
    const line = coordinates.map(({ x, y }) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
    const hoverPoints = coordinates.map(({ point, x, y }) => ({ x: Number(x.toFixed(1)), y: Number(y.toFixed(1)), primary: `$${money(point.price_usd)} · €${money(point.price_eur)}`, secondary: formatLocalDateTime(point.recorded_at) }));
    const first = clean[0], last = clean[clean.length - 1];
    const change = (Number(last.price_usd) / Number(first.price_usd) - 1) * 100;
    const changeClass = change >= 0 ? 'up' : 'down';
    const ariaLabel = `${chartRangeLabel(range)} USD price chart from ${formatChartDate(first.recorded_at)} to ${formatChartDate(last.recorded_at)}`;
    const chart = interactiveChartSvg({ className: 'history-chart', width, height, grid: `<path class="history-grid" d="M0 20H${width} M0 41H${width} M0 62H${width}"/>`, lineClass: `history-line ${changeClass}`, linePoints: line, hoverPoints, ariaLabel });
    return `<div class="history-meta"><span>$${money(min)} – $${money(max)}</span><span class="history-change ${changeClass}">${change >= 0 ? '+' : ''}${change.toFixed(2)}%</span></div>${chart}<div class="history-dates"><span>${formatChartDate(first.recorded_at)}</span><span>${formatChartDate(last.recorded_at)}</span></div>`;
}

function renderHistoryPanel(panel, history, range) {
    panel.innerHTML = `<div class="history-header"><div class="history-title">${chartRangeLabel(range)} · USD <span>${history.points.length} local samples</span></div>${chartRangeControls(range)}</div>${renderHistoryChart(history.points, range)}`;
    attachInteractiveLineCharts(panel);
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
    container.querySelectorAll('.stock-card[data-stock-id]').forEach(card => {
        card.addEventListener('mouseenter', () => loadCardHistory(card, '1mo'), { once: true });
        card.addEventListener('focusin', () => loadCardHistory(card, '1mo'), { once: true });
        card.addEventListener('touchstart', () => {
            const panel = card.querySelector('.history-popover');
            panel.classList.add('is-open');
            loadCardHistory(card, panel.dataset.requestedRange || '1mo');
        }, { passive: true, once: true });
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
    // The Dashboard is the active monitoring view. Disabled stocks remain in
    // Configure, but are intentionally excluded from every dashboard metric
    // and card.
    const monitoredStocks = stocks.filter(stock => Number(stock.enabled) === 1);
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

    container.innerHTML = `<header><h1>📊 OpenStock</h1><p class="subtitle">Monitoring ${enabledCount} stocks · ${ownedCount} owned</p></header><div id="ac"></div>${triggerCount ? `<div class="alert-box success">⚡ ${triggerCount} trigger(s) active</div>` : ''}<div class="summary"><div class="summary-card"><div class="summary-label">Monitored</div><div class="summary-value">${enabledCount}/25</div></div><div class="summary-card"><div class="summary-label">Owned</div><div class="summary-value">${ownedCount}</div></div><div class="summary-card"><div class="summary-label">Triggers</div><div class="summary-value">${triggerCount}</div></div></div><div class="last-update">Last: ${formatLocalDateTime(lastUpdate)} · Open a stock chart with mouse, keyboard focus, or a tap.</div>${monitoredStocks.length ? `<div class="stock-grid">${monitoredStocks.map(stock => `<div class="stock-card ${stock.owned ? '' : 'sold'} ${stock.trigger_state === 'BUY_TRIGGERED' ? 'trigger-buy' : ''} ${stock.trigger_state === 'SELL_TRIGGERED' ? 'trigger-sell' : ''}" data-stock-id="${stock.id}" tabindex="0"><div class="stock-symbol">${stock.symbol} <span class="badge ${stock.owned ? 'owned' : 'sold'}">${stock.owned ? 'OWNED' : 'SOLD'}</span> <span class="badge enabled">ON</span> ${stock.trigger_state === 'BUY_TRIGGERED' ? '<span class="badge buy-trigger">🔼 BUY</span>' : ''} ${stock.trigger_state === 'SELL_TRIGGERED' ? '<span class="badge sell-trigger">🔽 SELL</span>' : ''}</div><div class="stock-name">${stock.name || ''}</div><div class="stock-price eu">€${money(stock.current_price_eur)}</div><div class="stock-price usd">$${money(stock.current_price_usd)}</div><div class="thresholds"><span class="threshold buy">Buy €${money(stock.buy_threshold_eur)}</span><span class="threshold sell">Sell €${money(stock.sell_threshold_eur)}</span></div><div class="stock-actions"><button class="btn btn-sm btn-secondary" onclick="tos('${stock.symbol}')">${stock.owned ? 'Mark Sold' : 'Mark Owned'}</button><button class="btn btn-sm btn-secondary" onclick="ts('${stock.symbol}')">Disable</button><button class="btn btn-sm btn-danger" onclick="rs('${stock.symbol}')">Remove</button></div><div class="stock-footer">${stock.last_price_update ? `Updated ${formatLocalDateTime(stock.last_price_update)}` : 'Never updated'}</div><div class="history-popover" role="status" aria-live="polite"></div></div>`).join('')}</div>` : '<div class="card empty-state">No stocks are enabled. Enable a stock in Configure to monitor it here.</div>'}<div style="margin-top:2rem"><button class="btn btn-primary" onclick="nav('#/config')">+ Add Stock</button> <button class="btn btn-secondary" onclick="refreshPrices()">Refresh Prices</button></div>`;
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
        showA(result.archived ? `Archived ${symbol}; journal history retained` : `Removed ${symbol}`, 'success');
    }
}

function journalEscape(value) {
    return String(value ?? '').replace(/[&<>'"]/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[character]));
}

function journalMoney(value) {
    return value === null || value === undefined || !Number.isFinite(Number(value)) ? 'N/A' : `€${Number(value).toFixed(2)}`;
}

function journalPercent(value) {
    return value === null || value === undefined || !Number.isFinite(Number(value)) ? 'N/A' : `${Number(value).toFixed(2)}%`;
}

function journalSigned(value) {
    return value === null || value === undefined || !Number.isFinite(Number(value)) ? 'N/A' : `${Number(value) > 0 ? '+' : ''}${journalMoney(value)}`;
}

function journalClass(value) {
    return Number(value) > 0 ? 'positive' : Number(value) < 0 ? 'negative' : '';
}

function journalInputDate(value) {
    const date = parseOpenStockDate(value);
    if (!date) return '';
    const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
    return local.toISOString().slice(0, 16);
}

function journalKpi(label, displayValue, hint = '', cssClass = '') {
    return `<div class="journal-kpi" title="${journalEscape(hint)}"><div>${journalEscape(label)}</div><strong class="${cssClass}">${displayValue}</strong></div>`;
}

function renderEquityCurve(points, resolution = 'snapshot') {
    if (!points || points.length < 2) return '<div class="journal-empty">Insufficient data — equity snapshots begin after initial capital is recorded.</div>';
    const values = points.map(point => Number(point.equity_eur));
    const min = Math.min(...values), max = Math.max(...values), padding = Math.max((max - min) * 0.08, 1);
    const width = 800, height = 220, low = min - padding, high = max + padding;
    const times = points.map(point => parseOpenStockDate(point.at).getTime());
    const firstTime = times[0], timeRange = times[times.length - 1] - firstTime || 1;
    const coordinates = points.map((point, index) => ({ point, value: Number(point.equity_eur), x: (times[index] - firstTime) / timeRange * width, y: height - ((Number(point.equity_eur) - low) / (high - low || 1) * height) }));
    const line = coordinates.map(({ x, y }) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
    const hoverPoints = coordinates.map(({ point, value, x, y }) => ({ x: Number(x.toFixed(1)), y: Number(y.toFixed(1)), primary: `Equity ${journalMoney(value)}`, secondary: formatLocalDateTime(point.at), tertiary: `Cash ${journalMoney(point.cash_eur)} · Positions ${journalMoney(point.position_value_eur)}`, lineGap: 13 }));
    const ariaLabel = `Equity curve using ${resolution} closing samples from ${formatLocalDate(points[0].at)} to ${formatLocalDate(points[points.length - 1].at)}`;
    const chart = interactiveChartSvg({ className: 'journal-equity-curve', width, height, grid: `<path d="M0 55H${width} M0 110H${width} M0 165H${width}"/>`, lineClass: 'equity-line', linePoints: line, hoverPoints, ariaLabel });
    return `<div class="journal-chart-scale"><span>${points.length} ${resolution} samples</span><span>Hover or focus the line for exact values</span></div><div class="journal-chart-labels"><span>€${high.toFixed(0)}</span><span>€${low.toFixed(0)}</span></div>${chart}<div class="journal-chart-labels"><span>${formatLocalDate(points[0].at)}</span><span>Peak ${journalMoney(max)}</span><span>${formatLocalDate(points[points.length - 1].at)}</span></div>`;
}

function renderTradeRows(items) {
    if (!items.length) return '<tr><td colspan="8" style="color:#64748b">No trades recorded.</td></tr>';
    return items.map(trade => `<tr><td>${journalEscape(trade.trade_id)}</td><td><strong>${journalEscape(trade.symbol)}</strong></td><td><span class="badge ${trade.is_closed ? 'sold' : 'owned'}">${trade.is_closed ? 'CLOSED' : 'OPEN'}</span></td><td>${formatLocalDate(trade.opened_at)}</td><td>${trade.is_closed ? journalMoney(trade.dividend_eur) : '—'}</td><td class="${journalClass(trade.net_pnl_eur)}">${journalSigned(trade.net_pnl_eur)}</td><td class="${journalClass(trade.return_pct)}">${journalPercent(trade.return_pct)}</td><td><button class="btn btn-sm btn-secondary" onclick="editTrade(${trade.id})">Edit</button>${trade.is_closed ? '' : ` <button class="btn btn-sm btn-success" onclick="closeTrade(${trade.id})">Close</button>`}</td></tr>`).join('');
}

function renderJournal() {
    const container = document.getElementById('page-journal');
    queueJournalDividendEnhancements(container);
    const analytics = tradingAnalytics;
    const capital = analytics?.capital || {};
    const realized = analytics?.realized || {};
    const tradeStats = analytics?.trades || {};
    const performance = analytics?.performance || {};
    const risk = analytics?.risk || {};
    const edit = editingTradeId ? trades.find(trade => trade.id === editingTradeId) : null;
    const selectableStocks = stocks.filter(stock => (Number(stock.archived) !== 1 && Number(stock.enabled) === 1) || edit?.stock_id === stock.id);
    const options = selectableStocks.map(stock => `<option value="${stock.id}" ${edit?.stock_id === stock.id ? 'selected' : ''}>${journalEscape(stock.symbol)} — ${journalEscape(stock.name || '')}${stock.archived ? ' (archived history)' : ''}</option>`).join('');
    const currentPnl = (Number(realized.net_eur) || 0) + (Number(capital.unrealized_pnl_eur) || 0);
    container.innerHTML = `<header><h1>📓 Trading Journal</h1><p class="subtitle">Trades, performance and risk — separate from the stock-monitoring overview.</p></header><div id="ac"></div>${analytics?.equity_available ? '' : '<div class="alert-box info">Add an <strong>Initial capital</strong> event below to calculate current capital, return, exposure and an equity curve.</div>'}
      <div class="journal-kpi-grid">${journalKpi('Current Capital', journalMoney(capital.current_eur), 'Cash plus priced open positions.')}${journalKpi('Net P/L', journalSigned(currentPnl), 'Realized plus unrealized P/L; capital events excluded.', journalClass(currentPnl))}${journalKpi('Return', journalPercent(capital.current_return_pct), 'Current capital less net owner contributions divided by net owner contributions.', journalClass(capital.current_return_pct))}${journalKpi('YTD P/L', journalSigned(performance.ytd_pnl_eur), 'Net P/L from completed trades this calendar year.', journalClass(performance.ytd_pnl_eur))}${journalKpi('Win Rate', journalPercent(tradeStats.win_rate_pct), 'Winning completed trades divided by completed trades.')}${journalKpi('Max Drawdown', journalPercent(risk.max_drawdown_pct), 'Largest peak-to-trough decline in equity snapshots.', journalClass(risk.max_drawdown_pct))}${journalKpi('Available Cash', journalMoney(capital.available_cash_eur), 'Capital events less purchases/fees plus sales.')}${journalKpi('Open Positions', String(tradeStats.open ?? 0), 'Lots not yet closed.')}</div>
      <div class="card"><div class="section-header"><div><div class="section-title">Equity Curve</div><p class="subtitle">Current capital and peak equity over time.</p></div><div class="journal-small">Estimated yearly profit: ${journalMoney(performance.estimated_yearly_profit_eur)}<br>Annualized return: ${journalPercent(performance.annualized_return_pct)}</div></div>${renderEquityCurve(analytics?.equity_curve, analytics?.equity_curve_resolution)}</div>
      <div class="journal-columns"><div class="card"><div class="section-title">Trade Statistics</div><dl class="journal-metrics"><dt>Completed / winning / losing</dt><dd>${tradeStats.completed ?? 0} / ${tradeStats.winning ?? 0} / ${tradeStats.losing ?? 0}</dd><dt>Realized gains / losses</dt><dd><span class="positive">${journalSigned(realized.gains_eur)}</span> / <span class="negative">${journalSigned(realized.losses_eur)}</span></dd><dt>Net realized P/L</dt><dd class="${journalClass(realized.net_eur)}">${journalSigned(realized.net_eur)}</dd><dt>Average trade / profit factor</dt><dd>${journalSigned(tradeStats.expectancy_eur)} / ${tradeStats.profit_factor == null ? 'N/A' : Number(tradeStats.profit_factor).toFixed(2)}</dd><dt>Average holding / trades month</dt><dd>${tradeStats.average_holding_days == null ? 'N/A' : `${Number(tradeStats.average_holding_days).toFixed(1)} days`} / ${tradeStats.trades_per_month == null ? 'N/A' : Number(tradeStats.trades_per_month).toFixed(2)}</dd><dt>Volatility / Sharpe</dt><dd>${journalPercent(risk.volatility_pct)} / ${risk.sharpe_ratio == null ? 'Insufficient data' : Number(risk.sharpe_ratio).toFixed(2)}</dd></dl></div><div class="card"><div class="section-title">Monthly Performance</div>${analytics?.monthly_performance?.length ? `<div class="journal-table-wrap"><table class="table"><thead><tr><th>Month</th><th>P/L</th><th>Trades</th><th>Win Rate</th></tr></thead><tbody>${analytics.monthly_performance.slice(-12).reverse().map(row => `<tr><td>${row.month}</td><td class="${journalClass(row.pnl_eur)}">${journalSigned(row.pnl_eur)}</td><td>${row.trades}</td><td>${journalPercent(row.wins / row.trades * 100)}</td></tr>`).join('')}</tbody></table></div>` : '<p class="journal-empty">No completed trades yet.</p>'}</div></div>
      <div class="card"><div class="section-header"><div class="section-title">${edit ? `Edit ${journalEscape(edit.trade_id)}` : 'Record Trade'}</div>${edit ? '<button class="btn btn-sm btn-secondary" onclick="cancelTradeEdit()">Cancel</button>' : ''}</div><form onsubmit="saveTrade(event)"><div class="form-row"><div class="form-group"><label>Stock</label><select name="stock_id" required>${options}</select></div><div class="form-group"><label>Opened</label><input name="opened_at" type="datetime-local" required value="${journalInputDate(edit?.opened_at) || journalInputDate(new Date().toISOString())}"></div><div class="form-group"><label>Quantity</label><input name="quantity" type="number" min=".000001" step=".000001" required value="${edit?.quantity ?? ''}"></div></div><div class="form-row"><div class="form-group"><label>Currency</label><input name="currency" maxlength="3" required value="${journalEscape(edit?.currency || 'EUR')}"></div><div class="form-group"><label>Buy Price</label><input name="entry_price" type="number" min=".000001" step=".000001" required value="${edit?.entry_price ?? ''}"></div><div class="form-group"><label>EUR Rate at Buy</label><input name="entry_fx_rate" type="number" min=".000001" step=".000001" required value="${edit?.entry_fx_rate ?? 1}"></div><div class="form-group"><label>Buy Fees (€)</label><input name="entry_fee_eur" type="number" min="0" step=".01" value="${edit?.entry_fee_eur ?? 0}"></div></div><details ${edit?.closed_at ? 'open' : ''}><summary>Close trade now (optional)</summary><div class="form-row" style="margin-top:.75rem"><div class="form-group"><label>Closed</label><input name="closed_at" type="datetime-local" value="${journalInputDate(edit?.closed_at)}"></div><div class="form-group"><label>Sell Price</label><input name="exit_price" type="number" min=".000001" step=".000001" value="${edit?.exit_price ?? ''}"></div><div class="form-group"><label>EUR Rate at Sell</label><input name="exit_fx_rate" type="number" min=".000001" step=".000001" value="${edit?.exit_fx_rate ?? 1}"></div><div class="form-group"><label>Sell Fees (€)</label><input name="exit_fee_eur" type="number" min="0" step=".01" value="${edit?.exit_fee_eur ?? 0}"></div></div></details><div class="form-row" style="margin-top:.75rem"><div class="form-group"><label>Strategy / Reason</label><input name="strategy" value="${journalEscape(edit?.strategy || '')}"></div><div class="form-group"><label>Notes</label><input name="notes" value="${journalEscape(edit?.notes || '')}"></div></div><div class="btn-group"><button class="btn btn-primary">${edit ? 'Save Explicit Edit' : 'Record Trade'}</button></div></form></div>
      <div class="card"><div class="section-title">Capital Events</div><p class="subtitle">Capital events change cash/equity, never trading P/L.</p><form class="journal-capital-form" onsubmit="saveCapitalEvent(event)"><select name="event_type"><option value="INITIAL">Initial Capital</option><option value="DEPOSIT">Deposit</option><option value="WITHDRAWAL">Withdrawal</option><option value="FINANCING">Financing / Debt</option></select><input name="amount_eur" type="number" min=".01" step=".01" placeholder="Amount €" required><input name="occurred_at" type="datetime-local" value="${journalInputDate(new Date().toISOString())}" required><input name="notes" placeholder="Optional note"><button class="btn btn-primary">Add Event</button></form>${capitalEvents.length ? `<div class="journal-events">${capitalEvents.map(event => `<span><strong>${journalEscape(event.event_type)}</strong> ${journalMoney(event.amount_eur)} · ${formatLocalDate(event.occurred_at)}</span>`).join('')}</div>` : '<p class="journal-empty">No capital events recorded.</p>'}</div>
      <div class="card"><div class="section-title">Trades</div><div class="journal-table-wrap"><table class="table"><thead><tr><th>Trade ID</th><th>Stock</th><th>Status</th><th>Opened</th><th>Net P/L</th><th>Return</th><th></th></tr></thead><tbody>${renderTradeRows(trades)}</tbody></table></div></div>
      <div class="card"><div class="section-title">Per-Stock Analytics</div>${analytics?.stock_performance?.length ? `<div class="journal-table-wrap"><table class="table"><thead><tr><th>Stock</th><th>P/L</th><th>Trades</th><th>Win Rate</th><th>Average Return</th><th>Average Hold</th></tr></thead><tbody>${analytics.stock_performance.map(row => `<tr><td><strong>${journalEscape(row.symbol)}</strong></td><td class="${journalClass(row.pnl_eur)}">${journalSigned(row.pnl_eur)}</td><td>${row.trades}</td><td>${journalPercent(row.win_rate_pct)}</td><td>${journalPercent(row.average_return_pct)}</td><td>${Number(row.average_holding_days).toFixed(1)} days</td></tr>`).join('')}</tbody></table></div>` : '<p class="journal-empty">Per-stock analytics appear after completed trades.</p>'}</div><div class="card"><p class="journal-help">Definitions: Net P/L = sell EUR value − buy EUR value − entry fees − exit fees. Return = net P/L ÷ (buy EUR value + entry fees). Realized metrics use closed trades only; unrealized P/L uses current monitored EUR prices for open positions. Annualized estimates need at least 30 calendar days from initial capital; volatility and Sharpe require at least 30 equity observations.</p></div>`;
    attachInteractiveLineCharts(container);
}

function queueJournalDividendEnhancements(container) {
    queueMicrotask(() => {
        const closeDetails = container.querySelector('details');
        const closeRow = closeDetails?.querySelector('.form-row');
        if (closeRow && !closeRow.querySelector('[name="dividend_eur"]')) {
            const edit = editingTradeId ? trades.find(trade => trade.id === editingTradeId) : null;
            const dividends = Number.isFinite(Number(edit?.dividend_eur)) ? Number(edit.dividend_eur) : 0;
            closeRow.insertAdjacentHTML('beforeend', `<div class="form-group"><label>Dividends received (€)</label><input name="dividend_eur" type="number" min="0" step=".01" value="${dividends}"></div>`);
        }

        const tradesCard = [...container.querySelectorAll('.card')].find(card => card.querySelector('.section-title')?.textContent.trim() === 'Trades');
        const tradeHeaders = tradesCard?.querySelector('thead tr');
        if (tradeHeaders && ![...tradeHeaders.children].some(header => header.textContent === 'Dividends')) {
            const dividendHeader = document.createElement('th');
            dividendHeader.textContent = 'Dividends';
            const netPnlHeader = [...tradeHeaders.children].find(header => header.textContent === 'Net P/L');
            tradeHeaders.insertBefore(dividendHeader, netPnlHeader || null);
        }

        const statsCard = [...container.querySelectorAll('.card')].find(card => card.querySelector('.section-title')?.textContent.trim() === 'Trade Statistics');
        const metrics = statsCard?.querySelector('.journal-metrics');
        if (metrics && ![...metrics.querySelectorAll('dt')].some(term => term.textContent === 'Dividends received')) {
            metrics.insertAdjacentHTML('beforeend', `<dt>Dividends received</dt><dd class="positive">${journalSigned(tradingAnalytics?.realized?.dividends_eur)}</dd>`);
        }

        const help = container.querySelector('.journal-help');
        if (help) help.textContent = 'Definitions: Net P/L = sell EUR value + received dividends − buy EUR value − entry fees − exit fees. Return = net P/L ÷ (buy EUR value + entry fees). Realized metrics use closed trades only; unrealized P/L uses current monitored EUR prices for open positions. Annualized estimates need at least 30 calendar days from initial capital; volatility and Sharpe require at least 30 equity observations.';
    });
}

function formDataObject(form) {
    return Object.fromEntries(new FormData(form).entries());
}

async function saveTrade(event) {
    event.preventDefault();
    const body = formDataObject(event.currentTarget);
    if (!body.closed_at) {
        delete body.exit_price;
        delete body.exit_fx_rate;
        delete body.exit_fee_eur;
        delete body.dividend_eur;
    }
    const result = await af(editingTradeId ? `/trades/${editingTradeId}` : '/trades', { method: editingTradeId ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (!result) return;
    editingTradeId = null;
    await loadPage('journal');
    showA(result.message, 'success');
}

function editTrade(id) {
    editingTradeId = id;
    renderJournal();
    window.scrollTo({ top: 0, behavior: 'smooth' });
}

function cancelTradeEdit() {
    editingTradeId = null;
    renderJournal();
}

async function closeTrade(id) {
    const exit_price = prompt('Sell price in the trade currency:');
    if (exit_price === null) return;
    const exit_fx_rate = prompt('EUR conversion rate at sale (1 for EUR):', '1');
    if (exit_fx_rate === null) return;
    const exit_fee_eur = prompt('Sell fees in EUR:', '0');
    if (exit_fee_eur === null) return;
    const dividend_eur = prompt('Dividends received in EUR:', '0');
    if (dividend_eur === null) return;
    const result = await af(`/trades/${id}/close`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ exit_price, exit_fx_rate, exit_fee_eur, dividend_eur }) });
    if (!result) return;
    await loadPage('journal');
    showA('Trade closed', 'success');
}

async function saveCapitalEvent(event) {
    event.preventDefault();
    const result = await af('/capital-events', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(formDataObject(event.currentTarget)) });
    if (!result) return;
    await loadPage('journal');
    showA('Capital event recorded', 'success');
}

loadPage(pageFromHash());
