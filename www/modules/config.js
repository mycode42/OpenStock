import { escapeHtml } from './escape.js';
import { money } from './formatting.js';



export function createConfig(context) {
    let editingStockSymbol = null;
    const { getStocks, getStockMap, request: af, replaceStocks, upsertStock, removeStockFromState, beginStockMutation, isCurrentStockMutation, incrementStockRevision, showA, renderCurrentPage, pageFromHash, nav } = context;
function renderConfig() {
    const container = document.getElementById('page-config');
    container.innerHTML = `<header><h1>⚙️ Stock Configuration</h1><p class="subtitle">Manage monitoring stocks and independent alert levels</p></header><div id="ac"></div><div class="card"><div class="section-header"><div class="section-title">${getStocks().length}/25</div></div><div class="table-scroll"><table class="table"><thead><tr><th>Symbol</th><th>Name</th><th>Position</th><th>Stock</th><th></th></tr></thead><tbody>${getStocks().map(stock => { const position = stock.position || {}; return `<tr><td><strong>${escapeHtml(stock.symbol)}</strong></td><td>${escapeHtml(stock.name || '—')}</td><td><span class="badge ${position.owned ? 'owned' : 'sold'}">${position.owned ? 'HELD' : 'NOT HELD'}</span><small class="position-source">${escapeHtml(position.quantity ?? 0)} · ${escapeHtml(position.source || 'manual')}</small>${position.warning ? `<small class="warning-text">${escapeHtml(position.warning)}</small>` : ''}</td><td><span class="badge ${stock.enabled ? 'enabled' : 'disabled'}">${stock.enabled ? 'MONITORING' : 'DISABLED'}</span></td><td><button class="btn btn-sm btn-secondary" data-stock-action="edit" data-stock-symbol="${escapeHtml(stock.symbol)}">Edit name</button> <button class="btn btn-sm btn-secondary" data-stock-action="toggle" data-stock-symbol="${escapeHtml(stock.symbol)}">${stock.enabled ? 'Disable' : 'Enable'}</button> ${position.source === 'manual' ? `<button class="btn btn-sm btn-secondary" data-stock-action="owned" data-stock-symbol="${escapeHtml(stock.symbol)}">${position.owned ? 'Mark Sold (legacy)' : 'Mark Owned (legacy)'}</button>` : '<small class="journal-help">Journal-backed; use Journal</small>'} <button class="btn btn-sm btn-danger" data-stock-action="remove" data-stock-symbol="${escapeHtml(stock.symbol)}">Remove</button></td></tr>`; }).join('')}</tbody></table></div><div style="margin-top:1rem"><h3 id="stock-form-title">Add Stock</h3><p id="stock-form-help" class="journal-help">Initial BUY and SELL prices create the first alert levels. Existing-stock edits manage only the name; prices are managed below.</p><div class="form-row"><div class="form-group"><label>Symbol</label><input id="as-sym" placeholder="AAPL"></div><div class="form-group"><label>Name</label><input id="as-nm" placeholder="Apple Inc."></div></div><div class="form-row" id="initial-level-fields"><div class="form-group"><label for="as-buy">Initial BUY alert €</label><input id="as-buy" type="number" step=".01" min="0.01"></div><div class="form-group"><label for="as-sell">Initial SELL alert €</label><input id="as-sell" type="number" step=".01" min="0.01"></div></div><div class="btn-group"><button type="button" id="stock-form-submit" class="btn btn-primary" data-stock-editor-action="save">Add</button><button type="button" id="stock-form-cancel" class="btn btn-secondary hidden" data-stock-editor-action="cancel">Cancel</button></div></div><div id="as-result" style="margin-top:.75rem"></div></div>`;
    container.querySelectorAll('[data-stock-action]').forEach(button => button.addEventListener('click', () => ({ edit: es, owned: tos, toggle: ts, remove: rs }[button.dataset.stockAction])?.(button.dataset.stockSymbol)));
    container.querySelector('[data-stock-editor-action="save"]')?.addEventListener('click', () => editingStockSymbol ? esu(editingStockSymbol) : asm());
    container.querySelector('[data-stock-editor-action="cancel"]')?.addEventListener('click', cancelStockEdit);
    renderAlertLevelPanel(container);
    const archivedSection = document.createElement('div');
    archivedSection.id = 'archived-stocks';
    archivedSection.className = 'card';
    container.appendChild(archivedSection);
    populateStockEditor();
    loadArchivedStocks();
}

function renderAlertLevelPanel(container) {
    const panel = document.createElement('section');
    panel.className = 'card';
    panel.innerHTML = `<h2>Alert levels</h2><p class="journal-help">BUY and SELL levels are independent one-shot notifications. Delivered alerts retire only that level; alerts never place trades. Disabled/retired levels remain visible.</p>${getStocks().map(stock => `<div class="alert-level-stock" data-level-stock="${escapeHtml(stock.id)}"><div class="section-header"><h3>${escapeHtml(stock.symbol)}</h3><span class="position-summary">${escapeHtml(stock.position?.source || 'manual')} position: ${escapeHtml(stock.position?.quantity ?? 0)}${stock.position?.warning ? ` · ${escapeHtml(stock.position.warning)}` : ''}</span></div><div class="alert-level-list">${(stock.alert_levels || []).map(level => `<form class="alert-level-row" data-alert-form data-alert-id="${escapeHtml(level.id)}"><label>Side<select data-alert-side aria-label="${escapeHtml(stock.symbol)} alert side"><option value="BUY" ${level.side === 'BUY' ? 'selected' : ''}>BUY</option><option value="SELL" ${level.side === 'SELL' ? 'selected' : ''}>SELL</option></select></label><label>Price €<input type="number" min="0.01" step="0.01" data-alert-price value="${escapeHtml(level.price_eur)}" aria-label="${escapeHtml(stock.symbol)} alert price"></label><label class="checkbox-label"><input type="checkbox" data-alert-enabled ${level.enabled ? 'checked' : ''} ${level.retired ? 'disabled' : ''}> Enabled</label><span class="level-status">${level.retired ? 'Triggered / retired' : level.active && level.enabled ? 'Active' : 'Disabled'}</span><button type="submit" class="btn btn-sm btn-secondary" data-alert-action="save">Save</button>${level.retired ? '<button type="button" class="btn btn-sm btn-secondary" data-alert-action="rearm">Rearm</button>' : ''}<button type="button" class="btn btn-sm btn-danger" data-alert-action="delete">Delete</button></form>`).join('') || '<p class="journal-empty">No levels yet.</p>'}</div><form class="alert-level-add" data-alert-add-form><label>New side<select data-alert-side aria-label="New alert side"><option value="BUY">BUY</option><option value="SELL">SELL</option></select></label><label>Price €<input type="number" min="0.01" step="0.01" data-alert-price required></label><button type="submit" class="btn btn-primary" data-alert-action="add" data-stock-id="${escapeHtml(stock.id)}">Add level</button></form></div>`).join('')}`;
    container.appendChild(panel);
    panel.querySelectorAll('[data-alert-action="add"]').forEach(button => button.closest('form')?.addEventListener('submit', event => { event.preventDefault(); manageAlertLevel(button); }));
    panel.querySelectorAll('[data-alert-action="save"]').forEach(button => button.closest('form')?.addEventListener('submit', event => { event.preventDefault(); manageAlertLevel(button); }));
    panel.querySelectorAll('[data-alert-action="rearm"], [data-alert-action="delete"]').forEach(button => button.addEventListener('click', () => manageAlertLevel(button)));
}

async function manageAlertLevel(button) {
    const action = button.dataset.alertAction;
    let result;
    if (action === 'add') {
        const form = button.closest('form');
        const price = Number(form.querySelector('[data-alert-price]').value);
        if (!(price > 0)) return showA('Alert price must be positive', 'error');
        result = await af(`/stocks/${button.dataset.stockId}/alert-levels`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ side: form.querySelector('[data-alert-side]').value, price_eur: price }) });
    } else if (action === 'rearm') result = await af(`/alert-levels/${button.closest('[data-alert-id]').dataset.alertId}/rearm`, { method: 'POST' });
    else if (action === 'delete') result = await af(`/alert-levels/${button.closest('[data-alert-id]').dataset.alertId}`, { method: 'DELETE' });
    else if (action === 'save') { const row = button.closest('[data-alert-id]'); result = await af(`/alert-levels/${row.dataset.alertId}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ side: row.querySelector('[data-alert-side]').value, price_eur: Number(row.querySelector('[data-alert-price]').value), enabled: row.querySelector('[data-alert-enabled]').checked }) }); }
    if (result) { const stocks = await af('/stocks'); if (stocks) replaceStocks(stocks); renderConfig(); }
}

async function loadArchivedStocks() {
    const archived = await af('/stocks/archived');
    const container = document.getElementById('archived-stocks');
    if (!container || !archived) return;
    container.innerHTML = archived.length
        ? `<div class="section-title">Archived / historical stocks</div><p class="journal-help">Archived stocks stay out of monitoring and journal selectors until explicitly restored.</p><div class="journal-table-wrap"><table class="table"><thead><tr><th>Symbol</th><th>Name</th><th>Journal history</th><th></th></tr></thead><tbody>${archived.map(stock => `<tr><td><strong>${escapeHtml(stock.symbol)}</strong></td><td>${escapeHtml(stock.name || '—')}</td><td>${stock.has_journal_history ? 'Retained' : 'None'}</td><td><button type="button" class="btn btn-sm btn-secondary" data-restore-stock="${escapeHtml(stock.id)}">Restore</button></td></tr>`).join('')}</tbody></table></div>`
        : '<div class="section-title">Archived / historical stocks</div><p class="journal-empty">No archived stocks.</p>';
    container.querySelectorAll('[data-restore-stock]').forEach(button => button.addEventListener('click', () => restoreStock(button.dataset.restoreStock)));
}

async function restoreStock(id) {
    const result = await af(`/stocks/${encodeURIComponent(id)}/restore`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    if (!result?.stock) return;
    upsertStock(result.stock);
    renderConfig();
    showA('Stock restored and history backfill queued', 'success');
}

function populateStockEditor() {
    const stock = editingStockSymbol ? getStockMap()[editingStockSymbol] : null;
    if (!stock) {
        editingStockSymbol = null;
        return;
    }
    document.getElementById('stock-form-title').textContent = `Edit ${stock.symbol}`;
    document.getElementById('as-sym').value = stock.symbol;
    document.getElementById('as-sym').disabled = true;
    document.getElementById('as-nm').value = stock.name || '';
    document.getElementById('initial-level-fields').classList.add('hidden');
    document.getElementById('stock-form-submit').textContent = 'Save';
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
    incrementStockRevision();
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
    if (!getStockMap()[symbol]) return;
    editingStockSymbol = symbol;
    if (pageFromHash() === 'config') renderConfig();
    else nav('#/config');
}

async function esu(symbol) {
    const stock = getStockMap()[symbol];
    if (!stock) return;
    const name = document.getElementById('as-nm').value.trim();
    if (!name) {
        showA('Name required', 'error');
        return;
    }
    const token = beginStockMutation(stock.id);
    const result = await af(`/stocks/${stock.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name })
    });
    if (result?.stock && isCurrentStockMutation(stock.id, token)) {
        editingStockSymbol = null;
        upsertStock(result.stock);
        renderCurrentPage();
        showA(`Updated ${symbol}`, 'success');
    }
}

async function ts(symbol) {
    const stock = getStockMap()[symbol];
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
    const stock = getStockMap()[symbol];
    if (!stock) return;
    if (stock.position?.source === 'journal') {
        showA(`${symbol} is journal-backed; record or edit the journal trade`, 'error');
        return;
    }
    const token = beginStockMutation(stock.id);
    const result = await af(`/stocks/${stock.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ owned: stock.position?.owned ? 0 : 1 })
    });
    if (result?.stock && isCurrentStockMutation(stock.id, token)) {
        upsertStock(result.stock);
        renderCurrentPage();
        showA(`${symbol} marked ${result.stock.position?.owned ? 'owned' : 'sold'}`, 'success');
    }
}

async function rs(symbol) {
    const stock = getStockMap()[symbol];
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


    return { renderConfig, loadArchivedStocks, restoreStock, asm, cancelStockEdit, es, esu, ts, tos, rs };
}
