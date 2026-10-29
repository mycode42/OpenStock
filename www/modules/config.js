import { escapeHtml } from './escape.js';

const ORDER_TOKEN_KEY = 'openstock-order-token';

// Asks once per session for OPENSTOCK_ORDER_TOKEN. Returns '' when cancelled.
function orderToken() {
    let token = sessionStorage.getItem(ORDER_TOKEN_KEY);
    if (!token) {
        token = (window.prompt('OPENSTOCK_ORDER_TOKEN from .env') || '').trim();
        if (token) sessionStorage.setItem(ORDER_TOKEN_KEY, token);
    }
    return token;
}

function usd(value) {
    if (value === null || value === undefined || value === '' || !Number.isFinite(Number(value))) return '—';
    return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 6 }).format(Number(value));
}

function formatQuantity(value) {
    const quantity = Number(value);
    return Number.isFinite(quantity) ? quantity.toLocaleString(undefined, { maximumFractionDigits: 6 }) : '0';
}

export function createConfig({ request: af, reload, notify }) {
    let stocks = [];
    let orders = { mode: 'paper', notional_usd: 250 };
    let expandedStockId = null;
    let stockFilter = '';
    let liveTimer = null;
    let liveRequestInFlight = false;
    let liveAbortController = null;
    let pendingLevelId = null;
    const livePollMs = 1000;

    function stockById(id) {
        return stocks.find(stock => String(stock.id) === String(id));
    }

    function levelById(id) {
        return stocks.flatMap(stock => stock.alert_levels || []).find(level => String(level.id) === String(id));
    }

    function alertLabel(level) {
        if (level.trigger_mode === 'PERCENT') return `${level.side === 'BUY' ? 'DROP' : 'GAIN'} ${Number(level.change_pct).toFixed(2)}%`;
        return `${level.side} ${usd(level.price_usd)}`;
    }

    function levelSummary(stock) {
        const active = (stock.alert_levels || []).filter(level => level.enabled && level.active && !level.retired);
        if (!active.length) return '<span class="config-level-empty">No active levels</span>';
        return active.map(level => `<span class="threshold ${level.side === 'BUY' ? 'buy' : 'sell'}">${escapeHtml(alertLabel(level))}${level.auto_order ? ' · real order' : ''}</span>`).join('');
    }

    function positionSummary(stock) {
        const quantity = Number(stock.position?.quantity || 0);
        return quantity > 0
            ? `<span class="config-position held">${formatQuantity(quantity)} shares held</span>`
            : '<span class="config-position">No open Alpaca position</span>';
    }

    // Real-order switch. BUY shows its budget; SELL says it exits the whole position.
    function realOrderControl(level) {
        const id = escapeHtml(level.id);
        if (level.side === 'SELL') {
            return `<label class="config-switch config-real-order"><input type="checkbox" data-real-order="${id}" ${level.auto_order ? 'checked' : ''}><span>${level.auto_order ? 'Real order · sells whole position' : 'Real order when it fires'}</span></label>`;
        }
        const budget = level.auto_order && level.order_budget_usd
            ? `<span class="config-budget">${escapeHtml(usd(level.order_budget_usd))} budget</span><button type="button" class="btn btn-sm btn-secondary" data-budget="${id}">Edit budget</button>` : '';
        return `<label class="config-switch config-real-order"><input type="checkbox" data-real-order="${id}" ${level.auto_order ? 'checked' : ''}><span>Real order when it fires</span></label>${budget}`;
    }

    function openOrderNote(level) {
        const order = level.open_order;
        if (!order) return '';
        return `<small class="config-order-note">Order ${escapeHtml(order.status)} · ${escapeHtml(order.side)} ${escapeHtml(order.qty)} @ ${escapeHtml(usd(order.limit_price_usd))}</small>`;
    }

    function alertEditor(stock) {
        const levels = stock.alert_levels || [];
        const editable = levels.filter(level => !level.retired);
        const retired = levels.filter(level => level.retired);
        return `<div class="config-alert-editor" data-level-stock="${escapeHtml(stock.id)}">
            <div class="config-alert-heading"><div><strong>Price and percentage alerts</strong><p>Each alert sends one Telegram notification and retires. Turn on "Real order" to also place a day limit order when it fires.</p></div>${positionSummary(stock)}</div>
            <div class="config-alert-list">
                ${editable.map(level => level.trigger_mode === 'PERCENT'
                    ? `<form class="config-alert-row config-percent-row" data-alert-id="${escapeHtml(level.id)}"><div class="config-alert-description"><span class="threshold ${level.side === 'BUY' ? 'buy' : 'sell'}">${escapeHtml(alertLabel(level))}</span><small>From ${escapeHtml(usd(level.reference_price_usd))} · target ${escapeHtml(usd(level.price_usd))}</small>${openOrderNote(level)}</div><div class="config-percent-input"><input type="number" min=".01" max="${level.side === 'BUY' ? '99.99' : '9999.99'}" step=".01" data-alert-percent value="${escapeHtml(level.change_pct)}" aria-label="${escapeHtml(stock.symbol)} percentage move"><span>%</span></div><label class="config-switch"><input type="checkbox" data-alert-enabled ${level.enabled ? 'checked' : ''}><span>${level.enabled ? 'Active' : 'Paused'}</span></label>${realOrderControl(level)}<button type="submit" class="btn btn-sm btn-secondary" data-alert-action="save">Save</button><button type="button" class="btn btn-sm btn-danger" data-alert-action="delete">Delete</button></form>`
                    : `<form class="config-alert-row" data-alert-id="${escapeHtml(level.id)}"><select data-alert-side aria-label="${escapeHtml(stock.symbol)} alert side"><option value="BUY" ${level.side === 'BUY' ? 'selected' : ''}>BUY</option><option value="SELL" ${level.side === 'SELL' ? 'selected' : ''}>SELL</option></select><div class="config-price-input"><span>$</span><input type="number" min=".000001" step=".000001" data-alert-price value="${escapeHtml(level.price_usd)}" aria-label="${escapeHtml(stock.symbol)} alert price"></div><label class="config-switch"><input type="checkbox" data-alert-enabled ${level.enabled ? 'checked' : ''}><span>${level.enabled ? 'Active' : 'Paused'}</span></label>${realOrderControl(level)}${openOrderNote(level)}<button type="submit" class="btn btn-sm btn-secondary" data-alert-action="save">Save</button><button type="button" class="btn btn-sm btn-danger" data-alert-action="delete">Delete</button></form>`).join('') || '<p class="journal-empty">No active or paused alerts.</p>'}
                ${retired.length ? `<div class="config-retired"><div class="config-retired-title">Triggered · ready to rearm</div>${retired.map(level => `<div class="config-retired-row" data-alert-id="${escapeHtml(level.id)}"><span class="threshold ${level.side === 'BUY' ? 'buy' : 'sell'}">${escapeHtml(alertLabel(level))}${level.trigger_mode === 'PERCENT' ? ` · target ${escapeHtml(usd(level.price_usd))}` : ''}</span><span>${level.auto_order ? 'Real order on' : 'Notification only'}</span><button type="button" class="btn btn-sm btn-success" data-alert-action="rearm">Rearm</button><button type="button" class="btn btn-sm btn-danger" data-alert-action="delete">Delete</button></div>`).join('')}</div>` : ''}
            </div>
            <form class="config-alert-add" data-stock-id="${escapeHtml(stock.id)}"><select data-alert-side aria-label="New alert side"><option value="BUY">BUY</option><option value="SELL">SELL</option></select><div class="config-price-input"><span>$</span><input type="number" min=".000001" step=".000001" data-alert-price required placeholder="New price" aria-label="New alert price"></div><button type="submit" class="btn btn-sm btn-primary">Add price alert</button></form>
            <form class="config-percent-add" data-stock-id="${escapeHtml(stock.id)}"><label for="percent-direction-${escapeHtml(stock.id)}">Move</label><select id="percent-direction-${escapeHtml(stock.id)}" data-alert-direction aria-label="Percentage alert direction"><option value="DROP">Drop by</option><option value="GAIN">Gain by</option></select><div class="config-percent-input"><input type="number" min=".01" max="99.99" step=".01" data-alert-percent required placeholder="%" aria-label="Percentage move"><span>%</span></div><button type="submit" class="btn btn-sm btn-primary">Add percentage alert</button><small class="config-percent-help">Measured from the current Alpaca quote, then retires after one notification.</small></form>
        </div>`;
    }

    function stockCard(stock) {
        const expanded = String(expandedStockId) === String(stock.id);
        const retiredCount = (stock.alert_levels || []).filter(level => level.retired).length;
        const hidden = !`${stock.symbol} ${stock.name || ''}`.toLowerCase().includes(stockFilter.toLowerCase());
        return `<article class="config-stock-card ${expanded ? 'is-expanded' : ''}" data-stock-id="${escapeHtml(stock.id)}" ${hidden ? 'hidden' : ''}>
            <div class="config-stock-main">
                <div class="config-stock-identity"><strong>${escapeHtml(stock.symbol)}</strong><span>${escapeHtml(stock.name || '—')}</span><small data-live-price>${escapeHtml(usd(stock.current_price_usd))}</small></div>
                <div class="config-level-summary">${levelSummary(stock)}${retiredCount ? `<span class="config-retired-count">${retiredCount} triggered</span>` : ''}</div>
                <div class="config-stock-status">${positionSummary(stock)}<span class="badge ${stock.enabled ? 'enabled' : 'disabled'}">${stock.enabled ? 'Monitoring' : 'Paused'}</span></div>
                <div class="config-stock-actions"><button type="button" class="btn btn-sm ${expanded ? 'btn-primary' : 'btn-secondary'}" data-stock-action="levels" data-stock-id="${escapeHtml(stock.id)}" aria-expanded="${expanded}">${expanded ? 'Hide alerts' : 'Manage alerts'}</button></div>
            </div>${expanded ? alertEditor(stock) : ''}
        </article>`;
    }

    function render(nextStocks = stocks, nextOrders = orders) {
        stocks = nextStocks;
        orders = { ...orders, ...(nextOrders || {}) };
        const container = document.getElementById('page-config');
        if (!container.dataset.bound) bind(container);
        const activeCount = stocks.filter(stock => stock.enabled).length;
        const heldCount = stocks.filter(stock => Number(stock.position?.quantity) > 0).length;
        const realCount = stocks.reduce((total, stock) => total + (stock.alert_levels || []).filter(level => level.auto_order).length, 0);
        container.innerHTML = `<header class="config-header"><div><h1>Configure</h1><p class="subtitle">Alpaca watchlist: alert levels, real-order switches and one-time percentage moves.</p><div class="config-overview"><span><strong>${activeCount}</strong> monitored</span><span><strong>${heldCount}</strong> held</span><span><strong>${realCount}</strong> with real orders</span><span>Account: <strong>${escapeHtml(orders.mode)}</strong></span></div></div>
            <div class="config-search"><input id="config-stock-filter" type="search" placeholder="Filter symbols" aria-label="Filter symbols" value="${escapeHtml(stockFilter)}"></div></header>
            <div class="config-stock-list">${stocks.length ? stocks.map(stockCard).join('') : '<p class="journal-empty">No Alpaca watchlist symbols yet.</p>'}<div class="config-filter-empty empty-state" hidden>No symbols match this filter.</div></div>`;
        if (pendingLevelId) {
            const row = container.querySelector(`[data-alert-id="${CSS.escape(String(pendingLevelId))}"]`);
            row?.classList.add('is-focused');
            row?.scrollIntoView({ behavior: 'smooth', block: 'center' });
            pendingLevelId = null;
        }
        startLivePolling();
    }

    // Opens the stock's alerts and highlights one alert on the next render (deep link from the dashboard).
    function focus(stockId, levelId) {
        expandedStockId = stockId;
        pendingLevelId = levelId || null;
    }

    function bind(container) {
        container.dataset.bound = '1';
        container.addEventListener('click', event => {
            const button = event.target.closest('button');
            if (!button) return;
            if (button.dataset.stockAction === 'levels') return toggleLevels(button.dataset.stockId);
            if (button.dataset.budget) return editBudget(button.dataset.budget);
            const row = button.closest('[data-alert-id]');
            if (button.dataset.alertAction === 'rearm') return rearm(row.dataset.alertId, button);
            if (button.dataset.alertAction === 'delete') return deleteLevel(row.dataset.alertId, button);
        });
        container.addEventListener('change', event => {
            if (event.target.matches('[data-real-order]')) setRealOrder(event.target);
        });
        container.addEventListener('submit', event => {
            const form = event.target;
            if (form.matches('form[data-alert-id]')) { event.preventDefault(); return saveLevel(form); }
            if (form.matches('.config-alert-add')) { event.preventDefault(); return addPriceLevel(form); }
            if (form.matches('.config-percent-add')) { event.preventDefault(); return addPercentLevel(form); }
        });
        container.addEventListener('input', event => {
            if (event.target.id !== 'config-stock-filter') return;
            stockFilter = event.target.value.trim();
            let visible = 0;
            container.querySelectorAll('.config-stock-card').forEach(card => {
                const stock = stockById(card.dataset.stockId);
                card.hidden = !`${stock?.symbol || ''} ${stock?.name || ''}`.toLowerCase().includes(stockFilter.toLowerCase());
                if (!card.hidden) visible += 1;
            });
            const empty = container.querySelector('.config-filter-empty');
            if (empty) empty.hidden = visible > 0;
        });
    }

    function toggleLevels(stockId) {
        expandedStockId = String(expandedStockId) === String(stockId) ? null : stockId;
        render();
    }

    async function mutate(path, options, message) {
        const result = await af(path, options);
        if (!result) return null;
        notify?.(message, 'success');
        await reload?.();
        return result;
    }

    async function addPriceLevel(form) {
        const price = Number(form.querySelector('[data-alert-price]').value);
        if (!(price > 0)) return notify?.('Alert price must be positive', 'error');
        await mutate(`/alpaca/watchlist/${encodeURIComponent(form.dataset.stockId)}/alert-levels`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ side: form.querySelector('[data-alert-side]').value, price_usd: price }),
        }, 'Price alert added');
    }

    async function addPercentLevel(form) {
        const changePct = Number(form.querySelector('[data-alert-percent]').value);
        if (!(changePct > 0)) return notify?.('Percentage move must be positive', 'error');
        await mutate(`/alpaca/watchlist/${encodeURIComponent(form.dataset.stockId)}/percent-alerts`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ direction: form.querySelector('[data-alert-direction]').value, change_pct: changePct }),
        }, 'Percentage alert added');
    }

    async function saveLevel(form) {
        const levelId = form.dataset.alertId;
        const body = { enabled: form.querySelector('[data-alert-enabled]').checked };
        const percent = form.querySelector('[data-alert-percent]');
        if (percent) {
            const changePct = Number(percent.value);
            if (!(changePct > 0)) return notify?.('Percentage move must be positive', 'error');
            body.change_pct = changePct;
        } else {
            const price = Number(form.querySelector('[data-alert-price]').value);
            if (!(price > 0)) return notify?.('Alert price must be positive', 'error');
            body.side = form.querySelector('[data-alert-side]').value;
            body.price_usd = price;
        }
        await mutate(`/alpaca/alert-levels/${encodeURIComponent(levelId)}`, {
            method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
        }, 'Alert saved');
    }

    async function rearm(levelId, button) {
        button.disabled = true;
        await mutate(`/alpaca/alert-levels/${encodeURIComponent(levelId)}/rearm`, { method: 'POST' }, 'Alert rearmed');
    }

    async function deleteLevel(levelId, button) {
        if (!window.confirm('Delete this alert level?')) return;
        button.disabled = true;
        await mutate(`/alpaca/alert-levels/${encodeURIComponent(levelId)}`, { method: 'DELETE' }, 'Alert deleted');
    }

    // Asks for the budget when a BUY level is switched on or its budget is changed. Returns null when cancelled.
    function askBudget(current) {
        const entered = window.prompt('USD budget this BUY spends when the alert fires (whole shares, rounded down)', String(current || orders.notional_usd || 250));
        if (entered === null) return null;
        const amount = Number(entered);
        if (!Number.isFinite(amount) || amount <= 0) { notify?.('Enter a positive USD budget', 'error'); return null; }
        return amount;
    }

    async function setOrderSettings(levelId, autoOrder, budgetUsd) {
        // The order token confirms a change on a live account only. Paper needs none.
        const live = orders.mode === 'live';
        const token = live && autoOrder ? orderToken() : '';
        if (live && autoOrder && !token) return null;
        const body = { auto_order: autoOrder, ...(budgetUsd === undefined ? {} : { budget_usd: budgetUsd }) };
        const headers = { 'Content-Type': 'application/json', ...(token ? { 'X-OpenStock-Order-Token': token } : {}) };
        const result = await af(`/alpaca/alert-levels/${encodeURIComponent(levelId)}/order`, {
            method: 'PUT',
            headers,
            body: JSON.stringify(body),
        });
        if (!result) {
            if (live) sessionStorage.removeItem(ORDER_TOKEN_KEY);
            return null;
        }
        return result;
    }

    async function setRealOrder(box) {
        const levelId = box.dataset.realOrder;
        const enabling = box.checked;
        const level = levelById(levelId);
        if (!level) return;
        let budget;
        if (enabling) {
            if (level.side === 'BUY') {
                budget = askBudget(level.order_budget_usd);
                if (budget === null) { box.checked = false; return; }
            } else if (!window.confirm(`When this SELL fires, OpenStock sells the whole open position with a day limit order on the ${orders.mode} account. Turn it on?`)) {
                box.checked = false;
                return;
            }
        }
        box.disabled = true;
        const result = await setOrderSettings(levelId, enabling, budget);
        if (!result) { box.checked = !enabling; box.disabled = false; return; }
        notify?.(enabling ? `Real order armed on the ${orders.mode} account` : 'Real order switched off', 'success');
        await reload?.();
    }

    async function editBudget(levelId) {
        const level = levelById(levelId);
        if (!level) return;
        const budget = askBudget(level.order_budget_usd);
        if (budget === null) return;
        const result = await setOrderSettings(levelId, true, budget);
        if (!result) return;
        notify?.('Order budget saved', 'success');
        await reload?.();
    }

    async function refreshLive() {
        const payload = await af('/live-prices');
        if (!payload) return;
        const container = document.getElementById('page-config');
        for (const quote of payload.prices || []) {
            const price = container?.querySelector(`.config-stock-card[data-stock-id="${quote.stock_id}"] [data-live-price]`);
            if (price) price.textContent = usd(quote.price_usd);
        }
    }

    async function pollLivePrices() {
        if (liveRequestInFlight || document.hidden || window.location.hash !== '#/config') return;
        liveRequestInFlight = true;
        liveAbortController = new AbortController();
        try { await refreshLive(); }
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

    return { render, focus, stopLivePolling };
}
