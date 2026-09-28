import { escapeHtml } from './escape.js';
import { formatLocalDateTime, money } from './formatting.js';

function usd(value) { return Number.isFinite(Number(value)) ? `$${money(value)}` : '—'; }
function when(value) { return value ? formatLocalDateTime(value) : '—'; }
function bar(label, value) {
    if (!value) return '';
    return `<div class="alpaca-bar"><strong>${escapeHtml(label)}</strong><span>O ${usd(value.open_usd)} · H ${usd(value.high_usd)} · L ${usd(value.low_usd)} · C ${usd(value.close_usd)}</span><small>${when(value.time)} · Vol ${Number.isFinite(Number(value.volume)) ? Number(value.volume).toLocaleString('en-US') : '—'}</small></div>`;
}
function snapshotCard(row) {
    const snapshot = row.snapshot;
    if (!snapshot) return `<article class="card alpaca-card"><div class="stock-symbol">${escapeHtml(row.symbol)}</div><div class="stock-name">${escapeHtml(row.name)}</div><p class="journal-help">No Alpaca IEX snapshot is currently available for this symbol.</p></article>`;
    const quote = snapshot.latest_quote;
    const trade = snapshot.latest_trade;
    return `<article class="card alpaca-card"><div class="stock-symbol">${escapeHtml(row.symbol)}</div><div class="stock-name">${escapeHtml(row.name)} · IEX · USD</div><div class="alpaca-price">${usd(trade?.price_usd ?? (quote?.bid_usd && quote?.ask_usd ? (quote.bid_usd + quote.ask_usd) / 2 : null))}</div><div class="quote-sides"><span>Bid <strong>${usd(quote?.bid_usd)}</strong></span><span>Ask <strong>${usd(quote?.ask_usd)}</strong></span></div><div class="alpaca-detail"><span>Last trade: ${usd(trade?.price_usd)} · ${when(trade?.time)}</span><span>Quote: ${when(quote?.time)}</span></div>${bar('Minute bar', snapshot.minute_bar)}${bar('Today', snapshot.daily_bar)}${bar('Previous day', snapshot.previous_daily_bar)}</article>`;
}

export function createAlpaca(context) {
    const { request, showA } = context;
    let dashboard = null;
    async function refresh() {
        dashboard = await request('/alpaca/dashboard');
        render();
    }
    async function lookup(event) {
        event.preventDefault();
        const input = document.getElementById('alpaca-symbol');
        const result = document.getElementById('alpaca-lookup-result');
        const symbol = input?.value.trim().toUpperCase();
        if (!symbol) return;
        result.textContent = 'Loading…';
        const payload = await request(`/alpaca/snapshot/${encodeURIComponent(symbol)}`, {}, message => { result.textContent = message; });
        if (!payload?.snapshot) return;
        result.innerHTML = snapshotCard({ symbol: payload.snapshot.symbol, name: 'On-demand lookup', snapshot: payload.snapshot });
    }
    function render() {
        const container = document.getElementById('page-alpaca');
        const rows = dashboard?.rows || [];
        container.innerHTML = `<header class="section-header"><div><h1>Alpaca Market Data</h1><p class="subtitle">On-demand US market data · IEX feed · USD</p></div><button class="btn btn-secondary" id="alpaca-refresh">Refresh</button></header><div id="ac"></div><div class="alert-box info">Separate from OpenStock monitoring. Trade Republic remains the dashboard and alert source. This page uses read-only Alpaca market-data snapshots; it has no account, portfolio, order, or trading controls.</div><section class="card"><form id="alpaca-lookup" class="form-row"><div class="form-group"><label for="alpaca-symbol">US ticker</label><input id="alpaca-symbol" maxlength="32" autocomplete="off" placeholder="e.g. AAPL" aria-describedby="alpaca-help"></div><div class="form-group"><label>&nbsp;</label><button class="btn btn-primary" type="submit">Load snapshot</button></div></form><p id="alpaca-help" class="journal-help">Snapshot includes latest quote, latest trade, latest minute bar, daily bar, and previous daily bar. Values are indicative market data, not Trade Republic executable prices.</p><div id="alpaca-lookup-result" class="alpaca-lookup-result"></div></section><div class="last-update">${dashboard?.observed_at ? `Dashboard fetched ${when(dashboard.observed_at)}` : 'Loading Alpaca dashboard…'}</div><div class="alpaca-grid">${rows.length ? rows.map(snapshotCard).join('') : '<div class="card empty-state">No active OpenStock symbols to query.</div>'}</div>`;
        container.querySelector('#alpaca-refresh')?.addEventListener('click', refresh);
        container.querySelector('#alpaca-lookup')?.addEventListener('submit', lookup);
    }
    return { setDashboard: value => { dashboard = value; }, render, refresh };
}
