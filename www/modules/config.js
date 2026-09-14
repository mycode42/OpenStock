import { escapeHtml } from './escape.js';
import { money } from './formatting.js';



export function createConfig(context) {
    let editingStockSymbol = null;
    const { getStocks, getStockMap, request: af, replaceStocks, upsertStock, removeStockFromState, beginStockMutation, isCurrentStockMutation, incrementStockRevision, showA, renderCurrentPage, pageFromHash, nav } = context;
function renderConfig() {
    const container = document.getElementById('page-config');
    container.innerHTML = `<header><h1>⚙️ Stock Configuration</h1><p class="subtitle">Manage monitored stocks</p></header><div id="ac"></div><div class="card"><div class="section-header"><div class="section-title">${getStocks().length}/25</div></div><div style="overflow-x:auto"><table class="table"><thead><tr><th>Symbol</th><th>Name</th><th>€ Price</th><th>Buy</th><th>Sell</th><th>Status</th><th>On</th><th></th></tr></thead><tbody>${getStocks().map(stock => `<tr><td><strong>${escapeHtml(stock.symbol)}</strong></td><td>${escapeHtml(stock.name || '—')}</td><td>€${money(stock.current_price_eur)}</td><td>€${money(stock.buy_threshold_eur)}</td><td>€${money(stock.sell_threshold_eur)}</td><td><span class="badge ${stock.owned ? 'owned' : 'sold'}">${stock.owned ? 'OWNED' : 'SOLD'}</span></td><td><span class="badge ${stock.enabled ? 'enabled' : 'disabled'}">${stock.enabled ? 'ON' : 'OFF'}</span></td><td><button class="btn btn-sm btn-secondary" data-stock-action="edit" data-stock-symbol="${escapeHtml(stock.symbol)}">Edit</button> <button class="btn btn-sm btn-secondary" data-stock-action="owned" data-stock-symbol="${escapeHtml(stock.symbol)}">${stock.owned ? 'Mark Sold' : 'Mark Owned'}</button> <button class="btn btn-sm btn-secondary" data-stock-action="toggle" data-stock-symbol="${escapeHtml(stock.symbol)}">${stock.enabled ? 'Disable' : 'Enable'}</button> <button class="btn btn-sm btn-danger" data-stock-action="remove" data-stock-symbol="${escapeHtml(stock.symbol)}">Remove</button></td></tr>`).join('')}</tbody></table></div><div style="margin-top:1rem"><h3 id="stock-form-title">Add Stock</h3><div class="form-row"><div class="form-group"><label>Symbol</label><input id="as-sym" placeholder="AAPL"></div><div class="form-group"><label>Name</label><input id="as-nm" placeholder="Apple Inc."></div></div><div class="form-row"><div class="form-group"><label>Buy Target €</label><input id="as-buy" type="number" step=".01" min="0"></div><div class="form-group"><label>Sell Target €</label><input id="as-sell" type="number" step=".01" min="0"></div></div><div class="btn-group"><button type="button" id="stock-form-submit" class="btn btn-primary" data-stock-editor-action="save">Add</button><button type="button" id="stock-form-cancel" class="btn btn-secondary hidden" data-stock-editor-action="cancel">Cancel</button></div></div><div id="as-result" style="margin-top:.75rem"></div></div>`;
    container.querySelectorAll('[data-stock-action]').forEach(button => button.addEventListener('click', () => ({ edit: es, owned: tos, toggle: ts, remove: rs }[button.dataset.stockAction])?.(button.dataset.stockSymbol)));
    container.querySelector('[data-stock-editor-action="save"]')?.addEventListener('click', () => editingStockSymbol ? esu(editingStockSymbol) : asm());
    container.querySelector('[data-stock-editor-action="cancel"]')?.addEventListener('click', cancelStockEdit);
    const archivedSection = document.createElement('div');
    archivedSection.id = 'archived-stocks';
    archivedSection.className = 'card';
    container.appendChild(archivedSection);
    populateStockEditor();
    loadArchivedStocks();
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
    document.getElementById('as-buy').value = stock.buy_threshold_eur;
    document.getElementById('as-sell').value = stock.sell_threshold_eur;
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
