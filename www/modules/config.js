import { escapeHtml } from './escape.js';
import { money } from './formatting.js';

export function createConfig(context) {
    let editingStockSymbol = null;
    let stockFormOpen = false;
    let expandedStockId = null;
    let resolvedCryptoCandidates = [];
    let stockFilter = '';
    const { getStocks, getStockMap, request: af, replaceStocks, upsertStock, removeStockFromState, beginStockMutation, isCurrentStockMutation, incrementStockRevision, showA, renderCurrentPage, pageFromHash, nav } = context;
    let liveTimer = null;
    let liveRequestInFlight = false;
    let liveAbortController = null;
    const livePollMs = 1000;

    function instrumentPrice(stock, value) {
        if (value === null || value === undefined || value === '') return '—';
        if (stock?.asset_type !== 'crypto') return `€${money(value)}`;
        const price = Number(value);
        return Number.isFinite(price) ? new Intl.NumberFormat('de-DE', { style: 'currency', currency: 'EUR', minimumFractionDigits: 2, maximumFractionDigits: 12 }).format(price) : '—';
    }

    function applyLivePrices(payload) {
        const container = document.getElementById('page-config');
        for (const quote of payload?.prices || []) {
            const price = container?.querySelector(`.config-stock-card[data-stock-id="${quote.stock_id}"] [data-live-price]`);
            const stock = getStocks().find(item => String(item.id) === String(quote.stock_id));
            if (price) price.textContent = instrumentPrice(stock, quote.price_eur);
        }
    }

    async function pollLivePrices() {
        if (liveRequestInFlight || document.hidden || window.location.hash !== '#/config') return;
        liveRequestInFlight = true;
        liveAbortController = new AbortController();
        try { applyLivePrices(await af('/live-prices', { signal: liveAbortController.signal })); }
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

    function levelSummary(stock) {
        const active = (stock.alert_levels || []).filter(level => level.enabled && level.active && !level.retired);
        if (!active.length) return '<span class="config-level-empty">No active levels</span>';
        return active.map(level => `<span class="threshold ${level.side === 'BUY' ? 'buy' : 'sell'}">${escapeHtml(alertLabel(stock, level))}</span>`).join('');
    }

    function alertLabel(stock, level) {
        if (level.trigger_mode === 'PERCENT') return `${level.side === 'BUY' ? 'DROP' : 'GAIN'} ${Number(level.change_pct).toFixed(2)}%`;
        return `${level.side} ${instrumentPrice(stock, level.price_eur)}`;
    }

    function positionSummary(stock) {
        const quantity = Number(stock.position?.quantity || 0);
        const unit = stock.asset_type === 'crypto' ? 'units' : 'shares';
        return quantity > 0
            ? `<span class="config-position held">${formatQuantity(quantity)} ${unit} in Journal</span>`
            : '<span class="config-position">No open Journal position</span>';
    }

    function formatQuantity(value) {
        const quantity = Number(value);
        return Number.isFinite(quantity) ? quantity.toLocaleString(undefined, { maximumFractionDigits: 12 }) : '0';
    }

    function stockEditor() {
        if (!stockFormOpen && !editingStockSymbol) return '';
        const editing = editingStockSymbol ? getStockMap()[editingStockSymbol] : null;
        return `<section class="card config-editor-card" id="stock-editor">
            <div class="section-header"><div><h2>${editing ? `Edit ${escapeHtml(editing.symbol)}` : 'Add instrument'}</h2><p class="journal-help">${editing ? 'Update the display name. Alert prices are managed on the instrument card.' : 'Choose a Trade Republic stock or crypto asset. Initial prices create BUY and SELL alert levels.'}</p></div><button type="button" class="btn btn-sm btn-secondary" data-stock-editor-action="cancel">Close</button></div>
            ${editing ? `<div class="form-row"><div class="form-group"><label>Instrument ID</label><input value="${escapeHtml(editing.isin || 'legacy — no broker ID')}" disabled></div><div class="form-group"><label for="as-nm">Name</label><input id="as-nm" value="${escapeHtml(editing?.name || '')}"></div></div>` : '<div class="form-row"><div class="form-group"><label for="as-asset-type">Asset type</label><select id="as-asset-type"><option value="stock">Stock (ISIN)</option><option value="crypto">Crypto</option></select></div><div class="form-group"><label for="as-isin" id="as-identifier-label">Trade Republic ISIN</label><input id="as-isin" placeholder="12-character ISIN" maxlength="12" required><button type="button" class="btn btn-sm btn-secondary" id="resolve-isin">Resolve</button></div><div id="isin-preview" class="journal-help">Name and symbol are resolved from Trade Republic.</div></div><div class="form-row" id="crypto-candidate-row" hidden><div class="form-group"><label for="as-crypto-candidate">Crypto asset</label><select id="as-crypto-candidate"></select></div><div class="form-group"><label for="as-crypto-venue">Market</label><select id="as-crypto-venue"></select></div></div>'}
            ${editing ? '' : '<div class="form-row" id="initial-level-fields"><div class="form-group"><label for="as-buy">Initial BUY alert €</label><input id="as-buy" type="number" step=".000000000001" min=".000000000001"></div><div class="form-group"><label for="as-sell">Initial SELL alert €</label><input id="as-sell" type="number" step=".000000000001" min=".000000000001"></div></div>'}
            <div class="btn-group"><button type="button" class="btn btn-primary" data-stock-editor-action="save">${editing ? 'Save name' : 'Add instrument'}</button></div>
        </section>`;
    }

    function alertEditor(stock) {
        const levels = stock.alert_levels || [];
        const editable = levels.filter(level => !level.retired);
        const retired = levels.filter(level => level.retired);
        const currentPrice = Number(stock.current_price_eur);
        return `<div class="config-alert-editor" data-level-stock="${escapeHtml(stock.id)}">
            <div class="config-alert-heading"><div><strong>Price and percentage alerts</strong><p>Each alert sends one notification and retires. Rearm it to use the same target again.</p></div>${positionSummary(stock)}</div>
            <div class="config-alert-list">
                ${editable.map(level => level.trigger_mode === 'PERCENT'
                    ? `<form class="config-alert-row config-percent-row" data-alert-id="${escapeHtml(level.id)}"><div class="config-alert-description"><span class="threshold ${level.side === 'BUY' ? 'buy' : 'sell'}">${escapeHtml(alertLabel(stock, level))}</span><small>From ${instrumentPrice(stock, level.reference_price_eur)} · target ${instrumentPrice(stock, level.price_eur)}</small></div><div class="config-percent-input"><input type="number" min=".01" max="${level.side === 'BUY' ? '99.99' : '9999.99'}" step=".01" data-alert-percent value="${escapeHtml(level.change_pct)}" aria-label="${escapeHtml(stock.symbol)} percentage move"><span>%</span></div><label class="config-switch"><input type="checkbox" data-alert-enabled ${level.enabled ? 'checked' : ''}><span>${level.enabled ? 'Active' : 'Paused'}</span></label><button type="submit" class="btn btn-sm btn-secondary" data-alert-action="save">Save</button><button type="button" class="btn btn-sm btn-danger" data-alert-action="delete">Delete</button></form>`
                    : `<form class="config-alert-row" data-alert-id="${escapeHtml(level.id)}"><select data-alert-side aria-label="${escapeHtml(stock.symbol)} alert side"><option value="BUY" ${level.side === 'BUY' ? 'selected' : ''}>BUY</option><option value="SELL" ${level.side === 'SELL' ? 'selected' : ''}>SELL</option></select><div class="config-price-input"><span>€</span><input type="number" min=".000000000001" step=".000000000001" data-alert-price value="${escapeHtml(level.price_eur)}" aria-label="${escapeHtml(stock.symbol)} alert price"></div><label class="config-switch"><input type="checkbox" data-alert-enabled ${level.enabled ? 'checked' : ''}><span>${level.enabled ? 'Active' : 'Paused'}</span></label><button type="submit" class="btn btn-sm btn-secondary" data-alert-action="save">Save</button><button type="button" class="btn btn-sm btn-danger" data-alert-action="delete">Delete</button></form>`).join('') || '<p class="journal-empty">No active or paused alerts.</p>'}
                ${retired.length ? `<div class="config-retired"><div class="config-retired-title">Triggered · ready to rearm</div>${retired.map(level => `<div class="config-retired-row" data-alert-id="${escapeHtml(level.id)}"><span class="threshold ${level.side === 'BUY' ? 'buy' : 'sell'}">${escapeHtml(alertLabel(stock, level))}${level.trigger_mode === 'PERCENT' ? ` · target ${instrumentPrice(stock, level.price_eur)}` : ''}</span><span>${level.triggered_at ? `Triggered ${escapeHtml(level.triggered_at)}` : 'Triggered'}</span><button type="button" class="btn btn-sm btn-success" data-alert-action="rearm">Rearm</button><button type="button" class="btn btn-sm btn-danger" data-alert-action="delete">Delete</button></div>`).join('')}</div>` : ''}
            </div>
            <form class="config-alert-add"><select data-alert-side aria-label="New alert side"><option value="BUY">BUY</option><option value="SELL">SELL</option></select><div class="config-price-input"><span>€</span><input type="number" min=".000000000001" step=".000000000001" data-alert-price required placeholder="New price" aria-label="New alert price"></div><button type="submit" class="btn btn-primary" data-alert-action="add" data-stock-id="${escapeHtml(stock.id)}">Add level</button></form>
            <form class="config-percent-add"><label for="percent-direction-${escapeHtml(stock.id)}">Move</label><select id="percent-direction-${escapeHtml(stock.id)}" data-alert-direction aria-label="Percentage alert direction"><option value="DROP">Drop by</option><option value="GAIN">Gain by</option></select><div class="config-percent-input"><input type="number" min=".01" max="99.99" step=".01" data-alert-percent required placeholder="5" aria-label="Percentage move"><span>%</span></div><button type="submit" class="btn btn-primary" data-alert-action="add-percent" data-stock-id="${escapeHtml(stock.id)}" ${Number.isFinite(currentPrice) && currentPrice > 0 ? '' : 'disabled'}>Add move alert</button><small class="config-percent-help">From the latest saved quote: ${instrumentPrice(stock, stock.current_price_eur)}. Target price is calculated when added.</small></form>
        </div>`;
    }

    function stockCards() {
        if (!getStocks().length) return '<div class="empty-state">No instruments configured yet.</div>';
        const hasVisibleStock = getStocks().some(stock => `${stock.symbol} ${stock.name || ''}`.toLowerCase().includes(stockFilter.toLowerCase()));
        return `<div class="config-stock-list">${getStocks().map(stock => {
            const expanded = String(expandedStockId) === String(stock.id);
            const retiredCount = (stock.alert_levels || []).filter(level => level.retired).length;
            const journalOnly = !stock.broker_mapping?.verified_at;
            const hidden = !`${stock.symbol} ${stock.name || ''}`.toLowerCase().includes(stockFilter.toLowerCase());
            return `<article class="config-stock-card ${expanded ? 'is-expanded' : ''}" data-stock-id="${escapeHtml(stock.id)}" ${hidden ? 'hidden' : ''}>
                <div class="config-stock-main">
                    <div class="config-stock-identity"><strong>${escapeHtml(stock.symbol)}</strong><span>${escapeHtml(stock.name || '—')}</span><small data-live-price>${instrumentPrice(stock, stock.current_price_eur)}</small></div>
                    <div class="config-level-summary">${levelSummary(stock)}${retiredCount ? `<span class="config-retired-count">${retiredCount} triggered</span>` : ''}</div>
                    <div class="config-stock-status">${positionSummary(stock)}<span class="badge ${stock.enabled ? 'enabled' : 'disabled'}">${stock.enabled ? 'Monitoring' : journalOnly ? 'Journal only' : 'Disabled'}</span></div>
                    <div class="config-stock-actions"><button type="button" class="btn btn-sm ${expanded ? 'btn-primary' : 'btn-secondary'}" data-stock-action="levels" data-stock-symbol="${escapeHtml(stock.symbol)}" aria-expanded="${expanded}">${expanded ? 'Hide alerts' : 'Manage alerts'}</button><button type="button" class="btn btn-sm btn-secondary" data-stock-action="edit" data-stock-symbol="${escapeHtml(stock.symbol)}">Edit</button>${journalOnly ? '<span class="journal-help">Add this instrument from Trade Republic to enable monitoring.</span>' : `<button type="button" class="btn btn-sm btn-secondary" data-stock-action="toggle" data-stock-symbol="${escapeHtml(stock.symbol)}">${stock.enabled ? 'Disable' : 'Enable'}</button>`}<button type="button" class="btn btn-sm btn-danger" data-stock-action="remove" data-stock-symbol="${escapeHtml(stock.symbol)}">Remove</button></div>
                </div>${expanded ? alertEditor(stock) : ''}
            </article>`;
        }).join('')}<div class="config-filter-empty empty-state" ${hasVisibleStock ? 'hidden' : ''}>No instruments match this search.</div></div>`;
    }

    function renderConfig() {
        const container = document.getElementById('page-config');
        const activeCount = getStocks().filter(stock => stock.enabled).length;
        const heldCount = getStocks().filter(stock => Number(stock.position?.quantity) > 0).length;
        container.innerHTML = `<header class="config-header"><div><h1>⚙️ Configure</h1><p class="subtitle">Manage monitored stocks and crypto, alert levels, and one-time percentage moves.</p><div class="config-overview"><span><strong>${activeCount}</strong> / 25 monitored</span><span><strong>${heldCount}</strong> held in Journal</span><span><strong>${getStocks().length - activeCount}</strong> paused or Journal only</span></div></div><button type="button" class="btn btn-primary" data-config-action="add-stock">+ Add instrument</button></header><div id="ac"></div>${stockEditor()}<section class="card config-catalogue"><div class="section-header"><div><div class="section-title">Instruments</div><p class="journal-help">Choose Manage alerts to set price targets or percentage moves. Triggered alerts retire after one notification.</p></div><label class="config-search"><span class="sr-only">Search instruments</span><input id="config-stock-filter" type="search" placeholder="Search by symbol or name" value="${escapeHtml(stockFilter)}"></label></div>${stockCards()}</section><details class="card config-archive"><summary>Archived / historical instruments</summary><div id="archived-stocks"><p class="journal-empty">Loading…</p></div></details>`;
        container.querySelector('[data-config-action="add-stock"]')?.addEventListener('click', () => { editingStockSymbol = null; stockFormOpen = true; renderConfig(); focusEditor(); });
        container.querySelectorAll('[data-stock-action]').forEach(button => button.addEventListener('click', () => ({ edit: es, levels: toggleLevels, toggle: ts, remove: rs }[button.dataset.stockAction])?.(button.dataset.stockSymbol)));
        container.querySelector('[data-stock-editor-action="save"]')?.addEventListener('click', () => editingStockSymbol ? esu(editingStockSymbol) : asm());
        container.querySelector('[data-stock-editor-action="cancel"]')?.addEventListener('click', cancelStockEdit);
        container.querySelector('#resolve-isin')?.addEventListener('click', resolveIsin);
        container.querySelector('#as-asset-type')?.addEventListener('change', updateAssetTypeFields);
        container.querySelector('#as-crypto-candidate')?.addEventListener('change', populateCryptoVenues);
        container.querySelector('#as-isin')?.addEventListener('input', () => { resolvedCryptoCandidates = []; const row = document.getElementById('crypto-candidate-row'); if (row) row.hidden = true; });
        container.querySelectorAll('[data-alert-action="add"], [data-alert-action="add-percent"], [data-alert-action="save"]').forEach(button => button.closest('form')?.addEventListener('submit', event => { event.preventDefault(); manageAlertLevel(button); }));
        container.querySelectorAll('[data-alert-action="rearm"], [data-alert-action="delete"]').forEach(button => button.addEventListener('click', () => manageAlertLevel(button)));
        container.querySelectorAll('[data-alert-direction]').forEach(select => select.addEventListener('change', () => {
            const input = select.closest('form')?.querySelector('[data-alert-percent]');
            if (input) input.max = select.value === 'DROP' ? '99.99' : '9999.99';
        }));
        container.querySelector('#config-stock-filter')?.addEventListener('input', event => {
            stockFilter = event.currentTarget.value.trim();
            const list = container.querySelector('.config-stock-list');
            let visible = 0;
            list?.querySelectorAll('.config-stock-card').forEach(card => {
                const stock = getStocks().find(item => String(item.id) === card.dataset.stockId);
                card.hidden = !`${stock?.symbol || ''} ${stock?.name || ''}`.toLowerCase().includes(stockFilter.toLowerCase());
                if (!card.hidden) visible += 1;
            });
            const empty = container.querySelector('.config-filter-empty');
            if (empty) empty.hidden = visible > 0;
        });
        loadArchivedStocks();
        startLivePolling();
    }

    function updateAssetTypeFields() {
        const type = document.getElementById('as-asset-type')?.value || 'stock';
        const input = document.getElementById('as-isin');
        const label = document.getElementById('as-identifier-label');
        const row = document.getElementById('crypto-candidate-row');
        resolvedCryptoCandidates = [];
        if (row) row.hidden = true;
        if (label) label.textContent = type === 'crypto' ? 'Crypto name or symbol' : 'Trade Republic ISIN';
        if (input) {
            input.placeholder = type === 'crypto' ? 'ETH' : '12-character ISIN';
            input.maxLength = type === 'crypto' ? 64 : 12;
        }
        const preview = document.getElementById('isin-preview');
        if (preview) preview.textContent = type === 'crypto' ? 'Search Trade Republic crypto assets, then select the matching EUR market.' : 'Name and symbol are resolved from Trade Republic.';
    }

    function populateCryptoVenues() {
        const candidates = document.getElementById('as-crypto-candidate');
        const venues = document.getElementById('as-crypto-venue');
        const preview = document.getElementById('isin-preview');
        const candidate = resolvedCryptoCandidates[Number(candidates?.value)];
        if (!candidate || !venues) return;
        venues.innerHTML = candidate.venues.map((venue, index) => `<option value="${index}">${escapeHtml(venue.name)} · ${escapeHtml(venue.currency)}${venue.open ? ' · open' : ''}</option>`).join('');
        if (preview) preview.innerHTML = `<strong>${escapeHtml(candidate.name)} (${escapeHtml(candidate.symbol)})</strong> · ${escapeHtml(candidate.instrument_id)} · choose a EUR market. The selected market is checked for a live quote when saved.`;
    }

    async function resolveIsin() {
        const type = document.getElementById('as-asset-type')?.value || 'stock';
        const value = document.getElementById('as-isin')?.value.trim() || '';
        const preview = document.getElementById('isin-preview');
        if (type === 'crypto') {
            if (!value) return showA('Enter a crypto name or symbol', 'error');
            const result = await af('/instruments/resolve', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ asset_type: 'crypto', query: value }) });
            resolvedCryptoCandidates = result?.candidates || [];
            const row = document.getElementById('crypto-candidate-row');
            const select = document.getElementById('as-crypto-candidate');
            if (!resolvedCryptoCandidates.length || !row || !select) {
                if (preview) preview.textContent = 'No Trade Republic crypto with an EUR market was found.';
                return;
            }
            select.innerHTML = resolvedCryptoCandidates.map((asset, index) => `<option value="${index}">${escapeHtml(asset.symbol)} · ${escapeHtml(asset.name)} · ${escapeHtml(asset.instrument_id)}</option>`).join('');
            row.hidden = false;
            populateCryptoVenues();
            return;
        }
        if (!/^[A-Z0-9]{12}$/.test(value.toUpperCase())) return showA('A valid 12-character ISIN is required', 'error');
        const result = await af('/instruments/resolve', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ isin: value.toUpperCase() }) });
        if (result?.verified && preview) preview.innerHTML = `<strong>${escapeHtml(result.instrument.name)}</strong> · ${escapeHtml(result.instrument.ticker)} · ${escapeHtml(result.instrument.isin)} · ${escapeHtml(result.home.exchange)} · ${escapeHtml(result.home.currency)} · ${result.instrument.tradable === false ? 'not tradable' : 'tradable'}`;
    }

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
        } else if (action === 'add-percent') {
            const form = button.closest('form');
            const changePct = Number(form.querySelector('[data-alert-percent]').value);
            if (!(changePct > 0)) return showA('Percentage move must be positive', 'error');
            result = await af(`/stocks/${button.dataset.stockId}/percent-alerts`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ direction: form.querySelector('[data-alert-direction]').value, change_pct: changePct }) });
        } else if (action === 'save') {
            const percentInput = row.querySelector('[data-alert-percent]');
            const priceInput = row.querySelector('[data-alert-price]');
            const body = { enabled: row.querySelector('[data-alert-enabled]').checked };
            if (percentInput) {
                const changePct = Number(percentInput.value);
                if (!(changePct > 0)) return showA('Percentage move must be positive', 'error');
                body.change_pct = changePct;
            } else {
                const price = Number(priceInput.value);
                if (!(price > 0)) return showA('Alert price must be positive', 'error');
                body.side = row.querySelector('[data-alert-side]').value;
                body.price_eur = price;
            }
            result = await af(`/alert-levels/${row.dataset.alertId}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
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
        if (isNaN(buyThreshold) || isNaN(sellThreshold) || buyThreshold >= sellThreshold) return showA('Initial BUY price must be below the SELL price', 'error');
        const assetType = document.getElementById('as-asset-type')?.value || 'stock';
        let body;
        if (assetType === 'crypto') {
            const candidate = resolvedCryptoCandidates[Number(document.getElementById('as-crypto-candidate')?.value)];
            const selectedVenue = candidate?.venues?.[Number(document.getElementById('as-crypto-venue')?.value)];
            if (!candidate || !selectedVenue) return showA('Resolve a crypto asset and select its EUR market first', 'error');
            body = { asset_type: 'crypto', instrument_id: candidate.instrument_id, symbol: candidate.symbol, exchange: selectedVenue.exchange, buy_threshold_eur: buyThreshold, sell_threshold_eur: sellThreshold };
        } else {
            if (!/^[A-Z0-9]{12}$/.test(isin || '')) return showA('A valid 12-character ISIN is required', 'error');
            body = { isin, buy_threshold_eur: buyThreshold, sell_threshold_eur: sellThreshold };
        }
        incrementStockRevision();
        const result = await af('/stocks', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
        if (result?.stock) {
            stockFormOpen = false;
            resolvedCryptoCandidates = [];
            upsertStock(result.stock);
            expandedStockId = result.stock.id;
            renderConfig();
            showA(`${result.stock.enabled ? 'Monitoring' : 'Added'} ${result.stock.symbol}`, 'success');
        }
    }

    function es(symbol) {
        if (!getStockMap()[symbol]) return;
        editingStockSymbol = symbol;
        stockFormOpen = true;
        if (pageFromHash() === 'config') { renderConfig(); focusEditor(); } else nav('#/config');
    }

    function openAlerts(stockId) {
        const stock = getStocks().find(item => String(item.id) === String(stockId));
        if (!stock) return;
        expandedStockId = stock.id;
        stockFilter = stock.symbol;
        editingStockSymbol = null;
        stockFormOpen = false;
        if (pageFromHash() === 'config') {
            renderConfig();
            requestAnimationFrame(() => document.querySelector(`.config-stock-card[data-stock-id="${String(stock.id)}"]`)?.scrollIntoView({ behavior: 'smooth', block: 'center' }));
        } else nav('#/config');
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

    return { renderConfig, stopLivePolling, loadArchivedStocks, restoreStock, asm, cancelStockEdit, es, esu, ts, rs, openAlerts };
}
