import { escapeHtml } from './escape.js';
import { formatLocalDateTime } from './formatting.js';

const USD = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2 });

function price(value) {
    const number = value === null || value === undefined || value === '' ? NaN : Number(value);
    return Number.isFinite(number) ? USD.format(number) : '—';
}

function label(value) {
    return String(value || '').replaceAll('_', ' ').replace(/\b\w/g, character => character.toUpperCase()) || '—';
}

function quoteStatus(snapshot) {
    if (snapshot.quote_status === 'ok') return snapshot.demo_mode ? 'Simulated sample quotes' : `${snapshot.quote_feed || 'Alpaca'} · quotes received`;
    if (snapshot.quote_status === 'partial') return 'Some quotes unavailable';
    if (snapshot.quote_status === 'stale') return 'Alpaca unavailable · showing last quotes';
    if (snapshot.quote_status === 'empty') return 'No selected symbols to quote';
    return 'Alpaca quotes unavailable';
}

function membershipKey(snapshot) {
    return (snapshot?.symbols || []).map(item => `${item.symbol}|${item.category}|${item.sector}`).join(',');
}

export function createAlgoUniverse({ request, charts }) {
    const host = document.getElementById('page-algo-universe');
    let snapshot = null;
    let error = '';
    let filter = '';
    let pollTimer = null;
    let chartRefreshTimer = null;
    let chartRefreshInFlight = false;
    let requestInFlight = false;
    let abortController = null;
    let requestGeneration = 0;

    function renderCard(item) {
        const quote = item.quote || {};
        const timestamp = quote.timestamp ? formatLocalDateTime(quote.timestamp) : 'Quote unavailable';
        return `<article class="stock-card algo-universe-stock-card" data-quote-symbol="${escapeHtml(item.symbol)}" tabindex="0" aria-label="${escapeHtml(item.symbol)} quote card. Hover or focus for price history.">
            <div class="algo-universe-card-heading"><div class="stock-symbol">${escapeHtml(item.symbol)}</div><span class="algo-universe-category">${escapeHtml(label(item.category))}</span></div>
            <div class="stock-name algo-universe-sector">${escapeHtml(label(item.sector))}</div>
            <div class="stock-price usd algo-universe-midpoint" data-universe-midpoint>${price(quote.midpoint)}</div>
            <div class="quote-sides" aria-label="Alpaca bid and ask"><span>Bid <strong data-universe-bid>${price(quote.bid)}</strong></span><span>Ask <strong data-universe-ask>${price(quote.ask)}</strong></div>
            <div class="algo-universe-card-footer"><span>${snapshot?.demo_mode ? 'Simulated quote' : 'Alpaca SIP'}</span><time data-universe-quote-time>${escapeHtml(timestamp)}</time></div>
            <div class="history-popover" role="status" aria-live="polite"></div>
        </article>`;
    }

    function visibleSymbols() {
        const symbols = Array.isArray(snapshot?.symbols) ? snapshot.symbols : [];
        const query = filter.toLowerCase();
        return symbols.filter(item => !query || `${item.symbol} ${item.category} ${item.sector}`.toLowerCase().includes(query));
    }

    function renderCards() {
        const grid = host?.querySelector('.algo-universe-stock-grid');
        if (!grid || !snapshot) return;
        const rows = visibleSymbols();
        const count = host.querySelector('.algo-universe-result-count');
        if (count) count.textContent = `${rows.length} of ${snapshot.symbols.length} symbols`;
        grid.innerHTML = rows.length
            ? rows.map(renderCard).join('')
            : `<div class="card algo-universe-empty">${snapshot.symbols.length ? 'No symbols match this search.' : 'The current algo-trader universe is empty.'}</div>`;
        charts.attachQuoteChartHovers(host);
    }

    function render() {
        if (!host) return;
        const previousSearch = host.querySelector('#algo-universe-search');
        const wasFocused = previousSearch === document.activeElement;
        const selectionStart = previousSearch?.selectionStart;
        const selectionEnd = previousSearch?.selectionEnd;
        if (!snapshot) {
            host.innerHTML = `<main class="algo-universe-page">
                <header class="algo-universe-header"><div><span class="algo-universe-eyebrow">ALGO TRADER</span><h1>Current universe</h1><p class="subtitle">Selected equity symbols with Alpaca quotes and interactive price history.</p></div></header>
                ${error ? `<div class="card algo-universe-message error">${escapeHtml(error)} <button type="button" class="btn btn-sm btn-secondary" data-universe-refresh>Retry</button></div>` : '<div class="card algo-universe-message">Loading the current universe…</div>'}
            </main>`;
            return;
        }
        const symbols = Array.isArray(snapshot.symbols) ? snapshot.symbols : [];
        const selectionDate = snapshot.selector_as_of || snapshot.selection_date;
        const demoMode = snapshot.demo_mode === true;
        const emptyMessage = snapshot.selection_status === 'error'
            ? 'The selector could not load a current eligible universe. Check the Algo Trader status page.'
            : 'The current algo-trader universe is empty.';
        host.innerHTML = `<main class="algo-universe-page">
            <header class="algo-universe-header">
                <div><span class="algo-universe-eyebrow">${demoMode ? 'DEMO · SAMPLE EQUITY SELECTION' : 'ALGO TRADER · LIVE EQUITY SELECTION'}</span><h1>Current universe</h1><p class="subtitle">${demoMode ? 'Sample symbols and simulated quotes. Hover or focus a card to explore the example price history.' : 'Hover or focus a card to explore Alpaca price history. On touch screens, tap a card to open its chart.'}</p></div>
                <div class="algo-universe-header-actions"><span id="algo-universe-quote-status" class="algo-universe-status ${snapshot.quote_status === 'ok' ? 'ready' : ''}"><i aria-hidden="true"></i>${escapeHtml(quoteStatus(snapshot))}</span><button type="button" class="btn btn-sm btn-secondary" data-universe-refresh>Refresh</button></div>
            </header>
            <section class="algo-universe-summary" aria-label="Universe summary">
                <div class="algo-universe-summary-card"><span>Selected symbols</span><strong>${snapshot.symbol_count ?? symbols.length}</strong></div>
                <div class="algo-universe-summary-card"><span>Selection date</span><strong>${escapeHtml(selectionDate || '—')}</strong></div>
                <div class="algo-universe-summary-card"><span>Coordinator state</span><strong>${escapeHtml(label(snapshot.market_state))}</strong></div>
                <div class="algo-universe-summary-card"><span>Last scan</span><strong>${snapshot.last_scan_at ? escapeHtml(formatLocalDateTime(snapshot.last_scan_at)) : '—'}</strong></div>
            </section>
            ${snapshot.quote_error ? `<div class="algo-universe-quote-note">${escapeHtml(snapshot.quote_error)}</div>` : ''}
            <section class="algo-universe-card-section" aria-label="Selected algo-trader symbols">
                <div class="algo-universe-card-toolbar"><div><h2>Eligible symbols</h2><p class="algo-universe-result-count">${symbols.length} of ${symbols.length} symbols</p></div><label class="algo-universe-search"><span class="sr-only">Search the universe</span><input id="algo-universe-search" type="search" value="${escapeHtml(filter)}" placeholder="Search symbol, category, sector" autocomplete="off"></label></div>
                <div class="algo-universe-stock-grid">${symbols.length ? visibleSymbols().map(renderCard).join('') : `<div class="card algo-universe-empty">${escapeHtml(emptyMessage)}</div>`}</div>
                <footer class="algo-universe-footer"><span>${demoMode ? 'Sample data updated' : 'Quote refresh'} <time id="algo-universe-quote-updated">${snapshot.quotes_observed_at ? escapeHtml(formatLocalDateTime(snapshot.quotes_observed_at)) : 'unavailable'}</time></span><span>${demoMode ? 'Simulated USD quote values and history' : 'Quote values are in USD · history uses split-adjusted Alpaca bars'}</span></footer>
            </section>
        </main>`;
        charts.attachQuoteChartHovers(host);
        if (wasFocused) {
            const nextSearch = host.querySelector('#algo-universe-search');
            nextSearch?.focus();
            if (selectionStart !== null && selectionEnd !== null) nextSearch?.setSelectionRange(selectionStart, selectionEnd);
        }
    }

    function updateQuotesInPlace() {
        if (!host || !snapshot) return;
        const bySymbol = new Map(snapshot.symbols.map(item => [item.symbol, item]));
        host.querySelectorAll('.algo-universe-stock-card[data-quote-symbol]').forEach(card => {
            const item = bySymbol.get(card.dataset.quoteSymbol);
            if (!item) return;
            const quote = item.quote || {};
            const midpoint = card.querySelector('[data-universe-midpoint]');
            const bid = card.querySelector('[data-universe-bid]');
            const ask = card.querySelector('[data-universe-ask]');
            const time = card.querySelector('[data-universe-quote-time]');
            if (midpoint) midpoint.textContent = price(quote.midpoint);
            if (bid) bid.textContent = price(quote.bid);
            if (ask) ask.textContent = price(quote.ask);
            if (time) time.textContent = quote.timestamp ? formatLocalDateTime(quote.timestamp) : 'Quote unavailable';
        });
        const status = host.querySelector('#algo-universe-quote-status');
        if (status) {
            status.classList.toggle('ready', snapshot.quote_status === 'ok');
            status.innerHTML = `<i aria-hidden="true"></i>${escapeHtml(quoteStatus(snapshot))}`;
        }
        const updated = host.querySelector('#algo-universe-quote-updated');
        if (updated) updated.textContent = snapshot.quotes_observed_at ? formatLocalDateTime(snapshot.quotes_observed_at) : 'unavailable';
        const note = host.querySelector('.algo-universe-quote-note');
        if (snapshot.quote_error && note) note.textContent = snapshot.quote_error;
        else if (snapshot.quote_error) {
            const section = host.querySelector('.algo-universe-card-section');
            section?.insertAdjacentHTML('beforebegin', `<div class="algo-universe-quote-note">${escapeHtml(snapshot.quote_error)}</div>`);
        } else note?.remove();
    }

    async function load({ silent = false } = {}) {
        if (requestInFlight || document.hidden) return;
        requestInFlight = true;
        const generation = ++requestGeneration;
        abortController = new AbortController();
        if (!snapshot && !silent) render();
        const data = await request('/algo-trader/universe', { signal: abortController.signal }, () => {});
        if (generation !== requestGeneration) return;
        requestInFlight = false;
        abortController = null;
        if (!data) {
            if (!snapshot) error = 'OpenStock could not load the Algo universe endpoint. Check that the server has loaded the latest app code, then retry.';
            render();
            return;
        }
        const sameMembership = membershipKey(snapshot) === membershipKey(data);
        snapshot = data;
        error = '';
        if (sameMembership && host?.querySelector('.algo-universe-stock-grid')) updateQuotesInPlace();
        else render();
    }

    async function refreshOpenCharts({ includePreviouslyOpened = false } = {}) {
        if (chartRefreshInFlight || document.hidden || !host) return;
        chartRefreshInFlight = true;
        try { await charts.refreshOpenQuoteHistoryPanels(host, { includePreviouslyOpened }); }
        finally { chartRefreshInFlight = false; }
    }

    function startPolling() {
        if (pollTimer) return;
        load({ silent: Boolean(snapshot) });
        pollTimer = window.setInterval(() => load({ silent: true }), 15_000);
        chartRefreshTimer = window.setInterval(() => refreshOpenCharts(), 60_000);
    }

    function stopPolling() {
        if (pollTimer) window.clearInterval(pollTimer);
        if (chartRefreshTimer) window.clearInterval(chartRefreshTimer);
        pollTimer = null;
        chartRefreshTimer = null;
        requestGeneration += 1;
        requestInFlight = false;
        abortController?.abort();
        abortController = null;
    }

    host?.addEventListener('input', event => {
        if (event.target.id !== 'algo-universe-search') return;
        filter = event.target.value.trim();
        renderCards();
    });
    host?.addEventListener('click', event => {
        if (event.target.closest('[data-universe-refresh]')) load();
    });
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden && pollTimer) {
            load({ silent: true });
            refreshOpenCharts({ includePreviouslyOpened: true });
        }
    });

    return { render, startPolling, stopPolling };
}
