const API = './api';

let stocks = [];
let sMap = {};
let journalEntries = [];
let stockRevision = 0;
let journalRevision = 0;
let pageLoadId = 0;
let editingStockSymbol = null;
let pendingJournalStockId = null;
const stockMutationTokens = new Map();
const journalMutationTokens = new Map();

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

function replaceJournalEntries(entries) {
    journalEntries = [...entries].sort((a, b) => {
        const timeComparison = String(b.created_at).localeCompare(String(a.created_at));
        return timeComparison || b.id - a.id;
    });
}

function upsertJournalEntry(entry) {
    replaceJournalEntries([...journalEntries.filter(item => item.id !== entry.id), entry]);
}

function removeJournalEntryFromState(id) {
    replaceJournalEntries(journalEntries.filter(entry => entry.id !== id));
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

function beginJournalMutation(id) {
    const token = (journalMutationTokens.get(id) || 0) + 1;
    journalMutationTokens.set(id, token);
    journalRevision++;
    return token;
}

function isCurrentJournalMutation(id, token) {
    return journalMutationTokens.get(id) === token;
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
    const startingJournalRevision = journalRevision;
    showPage(page);
    document.getElementById(`page-${page}`).innerHTML = '<div class="loading">Loading...</div>';

    if (page === 'dashboard') {
        const [dashboard, entries] = await Promise.all([af('/dashboard'), af('/journal')]);
        if (loadId !== pageLoadId || pageFromHash() !== page) return;
        if (dashboard && stockRevision === startingStockRevision) replaceStocks(dashboard.stocks);
        if (entries && journalRevision === startingJournalRevision) replaceJournalEntries(entries);
        renderDashboard();
        return;
    }

    if (page === 'config') {
        const freshStocks = await af('/stocks');
        if (loadId !== pageLoadId || pageFromHash() !== page) return;
        if (freshStocks && stockRevision === startingStockRevision) replaceStocks(freshStocks);
        renderConfig();
        return;
    }

    const [freshStocks, entries] = await Promise.all([af('/stocks'), af('/journal')]);
    if (loadId !== pageLoadId || pageFromHash() !== page) return;
    if (freshStocks && stockRevision === startingStockRevision) replaceStocks(freshStocks);
    if (entries && journalRevision === startingJournalRevision) replaceJournalEntries(entries);
    renderJournal();
}

function renderCurrentPage() {
    const page = pageFromHash();
    showPage(page);
    if (page === 'dashboard') renderDashboard();
    else if (page === 'config') renderConfig();
    else renderJournal();
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

    container.innerHTML = `<header><h1>📊 OpenStock</h1><p class="subtitle">Monitoring ${enabledCount} stocks · ${ownedCount} owned · ${journalEntries.length} journal entries</p></header><div id="ac"></div>${triggerCount ? `<div class="alert-box success">⚡ ${triggerCount} trigger(s) active</div>` : ''}<div class="summary"><div class="summary-card"><div class="summary-label">Monitored</div><div class="summary-value">${enabledCount}/25</div></div><div class="summary-card"><div class="summary-label">Owned</div><div class="summary-value">${ownedCount}</div></div><div class="summary-card"><div class="summary-label">Journal</div><div class="summary-value">${journalEntries.length}</div></div></div><div class="last-update">Last: ${formatLocalDateTime(lastUpdate)}</div><div class="stock-grid">${stocks.map(stock => `<div class="stock-card ${stock.enabled ? '' : 'disabled'} ${stock.owned ? '' : 'sold'} ${stock.trigger_state === 'BUY_TRIGGERED' ? 'trigger-buy' : ''} ${stock.trigger_state === 'SELL_TRIGGERED' ? 'trigger-sell' : ''}"><div class="stock-symbol">${stock.symbol} <span class="badge ${stock.owned ? 'owned' : 'sold'}">${stock.owned ? 'OWNED' : 'SOLD'}</span> <span class="badge ${stock.enabled ? 'enabled' : 'disabled'}">${stock.enabled ? 'ON' : 'OFF'}</span> ${stock.trigger_state === 'BUY_TRIGGERED' ? '<span class="badge buy-trigger">🔼 BUY</span>' : ''} ${stock.trigger_state === 'SELL_TRIGGERED' ? '<span class="badge sell-trigger">🔽 SELL</span>' : ''}</div><div class="stock-name">${stock.name || ''}</div><div class="stock-price eu">€${money(stock.current_price_eur)}</div><div class="stock-price usd">$${money(stock.current_price_usd)}</div><div class="thresholds"><span class="threshold buy">Buy €${money(stock.buy_threshold_eur)}</span><span class="threshold sell">Sell €${money(stock.sell_threshold_eur)}</span></div><div class="stock-actions"><button class="btn btn-sm btn-secondary" onclick="ajm('${stock.symbol}')">+ Journal</button><button class="btn btn-sm btn-secondary" onclick="tos('${stock.symbol}')">${stock.owned ? 'Mark Sold' : 'Mark Owned'}</button><button class="btn btn-sm btn-secondary" onclick="ts('${stock.symbol}')">${stock.enabled ? 'Disable' : 'Enable'}</button><button class="btn btn-sm btn-danger" onclick="rs('${stock.symbol}')">Remove</button></div><div class="stock-footer">${stock.last_price_update ? `Updated ${formatLocalDateTime(stock.last_price_update)}` : 'Never updated'}</div></div>`).join('')}</div><div style="margin-top:2rem"><button class="btn btn-primary" onclick="nav('#/config')">+ Add Stock</button> <button class="btn btn-secondary" onclick="refreshPrices()">Refresh Prices</button></div><div style="margin-top:2rem"><div class="section-header"><div class="section-title">Recent</div><button class="btn btn-sm btn-primary" onclick="nav('#/journal')">View All</button></div><div class="card" style="overflow-x:auto"><table class="table"><thead><tr><th>Time</th><th>Stock</th><th>Action</th><th>€</th><th>Qty</th><th>Notes</th></tr></thead><tbody>${journalEntries.length ? journalEntries.slice(0, 10).map(entry => `<tr><td>${formatLocalDate(entry.created_at)}</td><td>${entry.symbol}</td><td><span class="badge ${entry.action === 'BUY' ? 'owned' : 'sold'}">${entry.action}</span></td><td>€${money(entry.price_eur)}</td><td>${entry.quantity}</td><td>${entry.notes || '—'}</td></tr>`).join('') : '<tr><td colspan="6" style="color:#64748b">No entries</td></tr>'}</tbody></table></div></div>`;
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

function ajm(symbol) {
    const stock = sMap[symbol];
    if (!stock) return;
    pendingJournalStockId = stock.id;
    if (pageFromHash() === 'journal') renderJournal();
    else nav('#/journal');
}

function renderJournal() {
    const container = document.getElementById('page-journal');
    container.innerHTML = `<header><h1>📓 Trading Journal</h1><p class="subtitle">${journalEntries.length} entries</p></header><div id="ac"></div><div class="card"><div class="section-header"><div class="section-title">New Entry</div></div><div class="form-row"><div class="form-group"><label>Stock</label><div class="dropdown" id="j-stock-d"><div class="dropdown-trigger" id="j-stock-trigger" onclick="dt('j-stock-d')">Select...</div><div class="dropdown-panel" id="j-stock-panel"></div><input type="hidden" id="j-stock"></div></div><div class="form-group"><label>Action</label><div class="dropdown" id="j-act-d"><div class="dropdown-trigger" id="j-act-trigger" onclick="dt('j-act-d')">BUY</div><div class="dropdown-panel" id="j-act-panel"></div><input type="hidden" id="j-act" value="BUY"></div></div></div><div class="form-row"><div class="form-group"><label>Price €</label><input id="j-px" type="number" step=".01" min=".01" placeholder="0.00"></div><div class="form-group"><label>Qty</label><input id="j-qty" type="number" step=".001" min=".001" placeholder="0.000"></div><div class="form-group"><label>Notes</label><input id="j-nt" placeholder="Reason..."></div></div><div class="btn-group"><button class="btn btn-success" onclick="aj()">+ Add Entry</button></div><div id="jr" style="margin-top:.75rem"></div></div><div class="card" style="margin-top:1rem;overflow-x:auto"><table class="table"><thead><tr><th>Time</th><th>Stock</th><th>Action</th><th>€</th><th>Qty</th><th>Notes</th><th></th></tr></thead><tbody>${journalEntries.length ? journalEntries.map(entry => `<tr><td>${formatLocalDate(entry.created_at)}</td><td><strong>${entry.symbol}</strong></td><td><span class="badge ${entry.action === 'BUY' ? 'owned' : 'sold'}">${entry.action}</span></td><td>€${money(entry.price_eur)}</td><td>${entry.quantity}</td><td>${entry.notes || '—'}</td><td><button class="btn btn-sm btn-secondary" onclick="ej(${entry.id})">Edit</button> <button class="btn btn-sm btn-danger" onclick="dj(${entry.id})">✕</button></td></tr>`).join('') : '<tr><td colspan="7" style="color:#64748b">No entries</td></tr>'}</tbody></table></div>`;

    if (stocks.length > 0) {
        const stockOptions = stocks.map(stock => ({ id: stock.id, symbol: stock.symbol }));
        const selectedIndex = Math.max(0, stockOptions.findIndex(stock => stock.id === pendingJournalStockId));
        di('j-stock', stockOptions, selectedIndex, stock => stock.symbol, stock => stock.id);
        di('j-act', ['BUY', 'SELL'], 0, action => action, action => action);
    }
    pendingJournalStockId = null;
}

async function aj() {
    const stockId = document.getElementById('j-stock').value;
    const action = document.getElementById('j-act').value || 'BUY';
    const price = parseFloat(document.getElementById('j-px').value);
    const quantity = parseFloat(document.getElementById('j-qty').value);
    const notes = document.getElementById('j-nt').value.trim();
    if (!stockId || isNaN(price) || price <= 0 || isNaN(quantity) || quantity <= 0) {
        showA('Fill all fields', 'error');
        return;
    }
    journalRevision++;
    const result = await af('/journal', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ stock_id: stockId, action, price_eur: price, quantity, notes })
    });
    if (result?.entry) {
        upsertJournalEntry(result.entry);
        renderJournal();
        showA('Entry added', 'success');
    }
}

function ej(id) {
    const entry = journalEntries.find(item => item.id === id);
    if (!entry) return;
    document.getElementById('journal-editor')?.remove();
    const container = document.getElementById('page-journal');
    const editor = document.createElement('div');
    editor.id = 'journal-editor';
    editor.className = 'card';
    editor.style.marginTop = '1rem';
    editor.innerHTML = `<h3>Edit Entry</h3><div class="form-row"><div class="form-group"><label>Action</label><div class="dropdown" id="ej-d"><div class="dropdown-trigger" id="ej-trigger" onclick="dt('ej-d')">${entry.action}</div><div class="dropdown-panel" id="ej-panel"></div><input type="hidden" id="ej" value="${entry.action}"></div></div><div class="form-group"><label>Price €</label><input id="ej-px" type="number" step=".01" value="${entry.price_eur}"></div><div class="form-group"><label>Qty</label><input id="ej-qty" type="number" step=".001" value="${entry.quantity}"></div><div class="form-group"><label>Notes</label><input id="ej-nt" value="${(entry.notes || '').replace(/"/g, '&quot;')}"></div></div><div class="btn-group"><button class="btn btn-primary" onclick="eju(${id})">Save</button><button class="btn btn-secondary" onclick="document.getElementById('journal-editor').remove()">Cancel</button></div>`;
    container.appendChild(editor);
    di('ej', ['BUY', 'SELL'], entry.action === 'BUY' ? 0 : 1, action => action, action => action);
}

async function eju(id) {
    const price = parseFloat(document.getElementById('ej-px').value);
    const quantity = parseFloat(document.getElementById('ej-qty').value);
    const notes = document.getElementById('ej-nt').value.trim();
    if (isNaN(price) || price <= 0 || isNaN(quantity) || quantity <= 0) {
        showA('Invalid values', 'error');
        return;
    }
    const token = beginJournalMutation(id);
    const result = await af(`/journal/${id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            action: document.getElementById('ej').value,
            price_eur: price,
            quantity,
            notes
        })
    });
    if (result?.entry && isCurrentJournalMutation(id, token)) {
        upsertJournalEntry(result.entry);
        renderJournal();
        showA('Updated', 'success');
    }
}

async function dj(id) {
    if (!confirm('Delete entry?')) return;
    const token = beginJournalMutation(id);
    const result = await af(`/journal/${id}`, { method: 'DELETE' });
    if (result && isCurrentJournalMutation(id, token)) {
        removeJournalEntryFromState(id);
        renderJournal();
        showA('Entry deleted', 'success');
    }
}

loadPage(pageFromHash());
