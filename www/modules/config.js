import { escapeHtml } from './escape.js';
import { money } from './formatting.js';

export function createConfig(context) {
    let editingStockSymbol = null;
    let stockFormOpen = false;
    let expandedStockId = null;
    const { getStocks, getStockMap, request: af, replaceStocks, upsertStock, removeStockFromState, beginStockMutation, isCurrentStockMutation, incrementStockRevision, showA, renderCurrentPage, pageFromHash, nav } = context;

    function levelSummary(stock) {
        const active = (stock.alert_levels || []).filter(level => level.enabled && level.active && !level.retired);
        if (!active.length) return '<span class="config-level-empty">No active levels</span>';
        return active.map(level => `<span class="threshold ${level.side === 'BUY' ? 'buy' : 'sell'}">${escapeHtml(level.side)} €${money(level.price_eur)}</span>`).join('');
    }

    function positionSummary(stock) {
        const quantity = Number(stock.position?.quantity || 0);
        return quantity > 0
            ? `<span class="config-position held">${money(quantity)} shares in Journal</span>`
            : '<span class="config-position">No open Journal position</span>';
    }

    function stockEditor() {
        if (!stockFormOpen && !editingStockSymbol) return '';
        const editing = editingStockSymbol ? getStockMap()[editingStockSymbol] : null;
        return `<section class="card config-editor-card" id="stock-editor">
            <div class="section-header"><div><h2>${editing ? `Edit ${escapeHtml(editing.symbol)}` : 'Add stock'}</h2><p class="journal-help">${editing ? 'Update the display name. Alert prices are managed on the stock card.' : 'The initial prices create the first BUY and SELL alert levels.'}</p></div><button type="button" class="btn btn-sm btn-secondary" data-stock-editor-action="cancel">Close</button></div>
            ${editing ? `<div class="form-row"><div class="form-group"><label>ISIN</label><input value="${escapeHtml(editing.isin || 'legacy — no ISIN')}" disabled></div><div class="form-group"><label for="as-nm">Name</label><input id="as-nm" value="${escapeHtml(editing?.name || '')}"></div></div>` : '<div class="form-row"><div class="form-group"><label for="as-isin">Trade Republic ISIN</label><input id="as-isin" placeholder="12-character ISIN" maxlength="12" required><button type="button" class="btn btn-sm btn-secondary" id="resolve-isin">Resolve</button></div><div id="isin-preview" class="journal-help">Name and symbol are resolved from Trade Republic.</div></div>'}
            ${editing ? '' : '<div class="form-row" id="initial-level-fields"><div class="form-group"><label for="as-buy">Initial BUY alert €</label><input id="as-buy" type="number" step=".01" min="0.01"></div><div class="form-group"><label for="as-sell">Initial SELL alert €</label><input id="as-sell" type="number" step=".01" min="0.01"></div></div>'}
            <div class="btn-group"><button type="button" class="btn btn-primary" data-stock-editor-action="save">${editing ? 'Save name' : 'Add stock'}</button></div>
        </section>`;
    }

    function alertEditor(stock) {
        const levels = stock.alert_levels || [];
        const editable = levels.filter(level => !level.retired);
        const retired = levels.filter(level => level.retired);
        return `<div class="config-alert-editor" data-level-stock="${escapeHtml(stock.id)}">
            <div class="config-alert-heading"><div><strong>Alert levels</strong><p>Each delivered level retires once. Rearm it whenever you want another notification.</p></div>${positionSummary(stock)}</div>
            <div class="config-alert-list">
                ${editable.map(level => `<form class="config-alert-row" data-alert-id="${escapeHtml(level.id)}"><select data-alert-side aria-label="${escapeHtml(stock.symbol)} alert side"><option value="BUY" ${level.side === 'BUY' ? 'selected' : ''}>BUY</option><option value="SELL" ${level.side === 'SELL' ? 'selected' : ''}>SELL</option></select><div class="config-price-input"><span>€</span><input type="number" min="0.01" step="0.01" data-alert-price value="${escapeHtml(level.price_eur)}" aria-label="${escapeHtml(stock.symbol)} alert price"></div><label class="config-switch"><input type="checkbox" data-alert-enabled ${level.enabled ? 'checked' : ''}><span>${level.enabled ? 'Active' : 'Paused'}</span></label><button type="submit" class="btn btn-sm btn-secondary" data-alert-action="save">Save</button><button type="button" class="btn btn-sm btn-danger" data-alert-action="delete">Delete</button></form>`).join('') || '<p class="journal-empty">No active or paused levels.</p>'}
                ${retired.length ? `<div class="config-retired"><div class="config-retired-title">Triggered · ready to rearm</div>${retired.map(level => `<div class="config-retired-row" data-alert-id="${escapeHtml(level.id)}"><span class="threshold ${level.side === 'BUY' ? 'buy' : 'sell'}">${escapeHtml(level.side)} €${money(level.price_eur)}</span><span>${level.triggered_at ? `Triggered ${escapeHtml(level.triggered_at)}` : 'Triggered'}</span><button type="button" class="btn btn-sm btn-success" data-alert-action="rearm">Rearm</button><button type="button" class="btn btn-sm btn-danger" data-alert-action="delete">Delete</button></div>`).join('')}</div>` : ''}
            </div>
            <form class="config-alert-add"><select data-alert-side aria-label="New alert side"><option value="BUY">BUY</option><option value="SELL">SELL</option></select><div class="config-price-input"><span>€</span><input type="number" min="0.01" step="0.01" data-alert-price required placeholder="New price" aria-label="New alert price"></div><button type="submit" class="btn btn-primary" data-alert-action="add" data-stock-id="${escapeHtml(stock.id)}">Add level</button></form>
        </div>`;
    }

    function stockCards() {
        if (!getStocks().length) return '<div class="empty-state">No stocks configured yet.</div>';
        return `<div class="config-stock-list">${getStocks().map(stock => {
            const expanded = String(expandedStockId) === String(stock.id);
            const retiredCount = (stock.alert_levels || []).filter(level => level.retired).length;
            return `<article class="config-stock-card ${expanded ? 'is-expanded' : ''}">
                <div class="config-stock-main">
                    <div class="config-stock-identity"><strong>${escapeHtml(stock.symbol)}</strong><span>${escapeHtml(stock.name || '—')}</span><small>€${money(stock.current_price_eur)}</small></div>
                    <div class="config-level-summary">${levelSummary(stock)}${retiredCount ? `<span class="config-retired-count">${retiredCount} triggered</span>` : ''}</div>
                    <div class="config-stock-status">${positionSummary(stock)}<span class="badge ${stock.enabled ? 'enabled' : 'disabled'}">${stock.enabled ? 'Monitoring' : 'Disabled'}</span><span class="badge enabled">${stock.isin ? `TR · ${escapeHtml(stock.isin)}` : 'Legacy · no TR mapping'}</span></div>
                    <div class="config-stock-actions"><button type="button" class="btn btn-sm ${expanded ? 'btn-primary' : 'btn-secondary'}" data-stock-action="levels" data-stock-symbol="${escapeHtml(stock.symbol)}" aria-expanded="${expanded}">${expanded ? 'Hide alerts' : 'Manage alerts'}</button>${stock.isin ? `<button type="button" class="btn btn-sm btn-secondary" data-stock-action="sync-tr-history" data-stock-id="${escapeHtml(stock.id)}">Sync TR history</button>` : ''}<button type="button" class="btn btn-sm btn-secondary" data-stock-action="edit" data-stock-symbol="${escapeHtml(stock.symbol)}">Edit</button><button type="button" class="btn btn-sm btn-secondary" data-stock-action="toggle" data-stock-symbol="${escapeHtml(stock.symbol)}">${stock.enabled ? 'Disable' : 'Enable'}</button><button type="button" class="btn btn-sm btn-danger" data-stock-action="remove" data-stock-symbol="${escapeHtml(stock.symbol)}">Remove</button></div>
                </div>${expanded ? alertEditor(stock) : ''}
            </article>`;
        }).join('')}</div>`;
    }

    function renderConfig() {
        const container = document.getElementById('page-config');
        container.innerHTML = `<header class="config-header"><div><h1>⚙️ Configure</h1><p class="subtitle">ISIN-first Trade Republic instruments, automatic ISIN resolution, and independent one-shot alert levels</p></div><button type="button" class="btn btn-primary" data-config-action="add-stock">+ Add stock</button></header><div id="ac"></div>${stockEditor()}<section class="card config-catalogue"><div class="section-header"><div><div class="section-title">Stocks</div><p class="journal-help">${getStocks().length}/25 configured · open one stock at a time to manage its alerts.</p></div></div>${stockCards()}</section><details class="card config-archive"><summary>Archived / historical stocks</summary><div id="archived-stocks"><p class="journal-empty">Loading…</p></div></details>`;
        container.querySelector('[data-config-action="add-stock"]')?.addEventListener('click', () => { editingStockSymbol = null; stockFormOpen = true; renderConfig(); focusEditor(); });
        container.querySelectorAll('[data-stock-action]').forEach(button => button.addEventListener('click', () => ({ edit: es, levels: toggleLevels, toggle: ts, remove: rs, 'sync-tr-history': syncTrHistory }[button.dataset.stockAction])?.(button.dataset.stockAction === 'sync-tr-history' ? button.dataset.stockId : button.dataset.stockSymbol)));
        container.querySelector('[data-stock-editor-action="save"]')?.addEventListener('click', () => editingStockSymbol ? esu(editingStockSymbol) : asm());
        container.querySelector('[data-stock-editor-action="cancel"]')?.addEventListener('click', cancelStockEdit);
        container.querySelector('#resolve-isin')?.addEventListener('click', resolveIsin);
        container.querySelectorAll('[data-alert-action="add"], [data-alert-action="save"]').forEach(button => button.closest('form')?.addEventListener('submit', event => { event.preventDefault(); manageAlertLevel(button); }));
        container.querySelectorAll('[data-alert-action="rearm"], [data-alert-action="delete"]').forEach(button => button.addEventListener('click', () => manageAlertLevel(button)));
        loadArchivedStocks();
    }

    async function resolveIsin() { const isin=document.getElementById('as-isin')?.value.trim().toUpperCase(); const preview=document.getElementById('isin-preview'); if(!/^[A-Z0-9]{12}$/.test(isin)) return showA('A valid 12-character ISIN is required','error'); const result=await af('/instruments/resolve',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({isin})}); if(result?.verified&&preview) preview.innerHTML=`<strong>${escapeHtml(result.instrument.name)}</strong> · ${escapeHtml(result.instrument.ticker)} · ${escapeHtml(result.instrument.isin)} · ${escapeHtml(result.home.exchange)} · ${escapeHtml(result.home.currency)} · ${result.instrument.tradable === false ? 'not tradable' : 'tradable'}`; }
    async function syncTrHistory(id) { const result=await af(`/stocks/${encodeURIComponent(id)}/history/sync-tr`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }); if (result?.source) showA(`Synchronized ${result.points} Trade Republic history points`, 'success'); }

    function focusEditor() {
        requestAnimationFrame(() => { const editor = document.getElementById('stock-editor'); editor?.scrollIntoView({ behavior: 'smooth', block: 'start' }); editor?.querySelector('input:not([disabled])')?.focus(); });
    }

    function toggleLevels(symbol) {
        const stock = getStockMap()[symbol];
        if (!stock) return;
        expandedStockId = String(expandedStockId) === String(stock.id) ? null : stock.id;
        renderConfig();
    }

    async function manageAlertLevel(button) {
        const action = button.dataset.alertAction;
        const row = button.closest('[data-alert-id]');
        let result;
        if (action === 'add') {
            const form = button.closest('form');
            const price = Number(form.querySelector('[data-alert-price]').value);
            if (!(price > 0)) return showA('Alert price must be positive', 'error');
            result = await af(`/stocks/${button.dataset.stockId}/alert-levels`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ side: form.querySelector('[data-alert-side]').value, price_eur: price }) });
        } else if (action === 'save') {
            const price = Number(row.querySelector('[data-alert-price]').value);
            if (!(price > 0)) return showA('Alert price must be positive', 'error');
            result = await af(`/alert-levels/${row.dataset.alertId}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ side: row.querySelector('[data-alert-side]').value, price_eur: price, enabled: row.querySelector('[data-alert-enabled]').checked }) });
        } else if (action === 'rearm') {
            result = await af(`/alert-levels/${row.dataset.alertId}/rearm`, { method: 'POST' });
        } else if (action === 'delete') {
            if (!confirm('Delete this alert level?')) return;
            result = await af(`/alert-levels/${row.dataset.alertId}`, { method: 'DELETE' });
        }
        if (!result) return;
        const stocks = await af('/stocks');
        if (stocks) replaceStocks(stocks);
        renderConfig();
    }

    async function loadArchivedStocks() {
        const archived = await af('/stocks/archived');
        const container = document.getElementById('archived-stocks');
        if (!container || !archived) return;
        container.innerHTML = archived.length
            ? `<p class="journal-help">Archived stocks stay out of monitoring and Journal selectors until restored.</p><div class="config-archived-list">${archived.map(stock => `<div><span><strong>${escapeHtml(stock.symbol)}</strong> · ${escapeHtml(stock.name || '—')}</span><button type="button" class="btn btn-sm btn-secondary" data-restore-stock="${escapeHtml(stock.id)}">Restore</button></div>`).join('')}</div>`
            : '<p class="journal-empty">No archived stocks.</p>';
        container.querySelectorAll('[data-restore-stock]').forEach(button => button.addEventListener('click', () => restoreStock(button.dataset.restoreStock)));
    }

    async function restoreStock(id) {
        const result = await af(`/stocks/${encodeURIComponent(id)}/restore`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
        if (!result?.stock) return;
        upsertStock(result.stock);
        renderConfig();
        showA('Stock restored and history backfill queued', 'success');
    }

    function cancelStockEdit() {
        editingStockSymbol = null;
        stockFormOpen = false;
        renderConfig();
    }

    async function asm() {
        const isin = document.getElementById('as-isin')?.value.trim().toUpperCase();
        const buyThreshold = parseFloat(document.getElementById('as-buy')?.value);
        const sellThreshold = parseFloat(document.getElementById('as-sell')?.value);
        if (!/^[A-Z0-9]{12}$/.test(isin)) return showA('A valid 12-character ISIN is required', 'error');
        if (isNaN(buyThreshold) || isNaN(sellThreshold) || buyThreshold >= sellThreshold) return showA('Initial BUY price must be below the SELL price', 'error');
        incrementStockRevision();
        const result = await af('/stocks', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ isin, buy_threshold_eur: buyThreshold, sell_threshold_eur: sellThreshold }) });
        if (result?.stock) {
            stockFormOpen = false;
            upsertStock(result.stock);
            expandedStockId = result.stock.id;
            renderConfig();
            showA(`Added ${result.stock.symbol}`, 'success');
        }
    }

    function es(symbol) {
        if (!getStockMap()[symbol]) return;
        editingStockSymbol = symbol;
        stockFormOpen = true;
        if (pageFromHash() === 'config') { renderConfig(); focusEditor(); } else nav('#/config');
    }

    async function esu(symbol) {
        const stock = getStockMap()[symbol];
        if (!stock) return;
        const name = document.getElementById('as-nm')?.value.trim();
        if (!name) return showA('Name required', 'error');
        const token = beginStockMutation(stock.id);
        const result = await af(`/stocks/${stock.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) });
        if (result?.stock && isCurrentStockMutation(stock.id, token)) {
            editingStockSymbol = null;
            stockFormOpen = false;
            upsertStock(result.stock);
            renderConfig();
            showA(`Updated ${symbol}`, 'success');
        }
    }

    async function ts(symbol) {
        const stock = getStockMap()[symbol];
        if (!stock) return;
        const token = beginStockMutation(stock.id);
        const result = await af(`/stocks/${stock.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: stock.enabled ? 0 : 1 }) });
        if (result?.stock && isCurrentStockMutation(stock.id, token)) {
            upsertStock(result.stock);
            renderConfig();
            showA(`${symbol} ${result.stock.enabled ? 'enabled' : 'disabled'}`, 'success');
        }
    }

    async function rs(symbol) {
        const stock = getStockMap()[symbol];
        if (!stock || !confirm(`Remove ${symbol}?`)) return;
        const token = beginStockMutation(stock.id);
        const result = await af(`/stocks/${stock.id}`, { method: 'DELETE' });
        if (result && isCurrentStockMutation(stock.id, token)) {
            if (editingStockSymbol === symbol) cancelStockEdit();
            removeStockFromState(stock.id);
            if (String(expandedStockId) === String(stock.id)) expandedStockId = null;
            renderCurrentPage();
            showA(result.archived ? `Archived ${symbol}; Journal history retained` : `Removed ${symbol}`, 'success');
        }
    }

    return { renderConfig, loadArchivedStocks, restoreStock, asm, cancelStockEdit, es, esu, ts, rs };
}
