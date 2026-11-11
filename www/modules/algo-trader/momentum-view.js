import { escapeHtml } from '../escape.js';
import { RANGES, localTimeZone, renderChart } from './equity-chart.js';
import { activityIcon, classForReturn, dateOnly, dateTime, money, signedMoney, signedPercent, timeOnly } from './format.js';
import { renderAccountSwitch } from './account-view.js';

// The momentum paper book, laid out like the Equity and Options pages. Its strategy panel is the basket, and its
// strategy table is the open positions. Read-only: orders come from the quarterly job.

const ACTIVITY_LIMIT = 12;

function switchMarkup(active) {
    return renderAccountSwitch({
        active, ariaLabel: 'Algo Trader paper account', attribute: 'data-account-mode',
        options: [['equity', 'Equity Account'], ['options', 'Options Account'], ['momentum', 'Momentum Account']],
    });
}

function unavailable(payload, error) {
    const reason = payload?.reason || error || 'The momentum paper account is unavailable right now.';
    return `<section class="algo-paused-notice"><strong>Account not reachable</strong><span>${escapeHtml(reason)} The basket and rebalance history below still come from the local log.</span></section>`;
}

function kpis(payload) {
    const metrics = payload.metrics || {};
    const latest = (payload.history || []).at(-1);
    const dayChange = Number.isFinite(payload.last_equity) ? payload.equity - payload.last_equity : null;
    return `<section class="algo-kpi-grid" aria-label="Momentum account KPIs">
        <article class="algo-kpi algo-kpi-featured"><div class="algo-kpi-label">ACCOUNT EQUITY <span class="algo-kpi-period">${escapeHtml(latest ? dateOnly(latest.date) : 'no history')}</span></div><strong>${money(payload.equity)}</strong><div class="algo-kpi-foot"><span>Cash ${escapeHtml(money(payload.cash))}</span><span>${escapeHtml(signedMoney(dayChange))} today</span></div></article>
        <article class="algo-kpi"><div class="algo-kpi-label">EQUITY CHANGE</div><strong class="${classForReturn(metrics.total_return_pct)}">${signedPercent(metrics.total_return_pct)}</strong><div class="algo-kpi-foot"><span>since first snapshot</span><span>${signedMoney(metrics.total_pnl)}</span></div></article>
        <article class="algo-kpi"><div class="algo-kpi-label">LATEST DAILY P&amp;L</div><strong class="${classForReturn(metrics.daily_pnl)}">${signedMoney(metrics.daily_pnl)}</strong><div class="algo-kpi-foot"><span class="${classForReturn(metrics.daily_pnl_pct)}">${signedPercent(metrics.daily_pnl_pct)}</span></div></article>
        <article class="algo-kpi"><div class="algo-kpi-label">MAX DRAWDOWN</div><strong class="algo-kpi-negative">${metrics.max_drawdown_pct === null || metrics.max_drawdown_pct === undefined ? '—' : `${Number(metrics.max_drawdown_pct).toFixed(2)}%`}</strong><div class="algo-kpi-foot"><span>over the history shown</span></div></article>
        <article class="algo-kpi"><div class="algo-kpi-label">OPEN POSITIONS</div><strong>${payload.positions.length}</strong><div class="algo-kpi-foot"><span>Basket names</span><span>${(payload.basket || []).length}</span></div></article>
    </section>`;
}

function chartPanel(payload, range, charts) {
    const history = payload.history || [];
    const metrics = payload.metrics || {};
    return `<article class="card algo-panel algo-chart-panel" data-chart-readout>
        <div class="algo-section-heading algo-chart-heading"><div><span class="algo-eyebrow">ACCOUNT PERFORMANCE</span><h2>Equity history</h2><p>${payload.demo_mode ? 'Simulated momentum account' : 'Momentum paper account'} · USD</p></div>
        <div class="algo-chart-summary"><strong data-chart-value data-default-value="${escapeHtml(money(payload.equity))}">${escapeHtml(money(payload.equity))}</strong><span data-chart-change class="${classForReturn(metrics.total_return_pct)}">${signedPercent(metrics.total_return_pct)}</span></div></div>
        <div class="algo-range-controls" role="group" aria-label="Chart time range">${RANGES.map(value => `<button type="button" class="algo-range-btn ${range === value ? 'active' : ''}" data-range="${value}" aria-pressed="${range === value}">${value}</button>`).join('')}</div>
        <div class="algo-chart-wrap">${renderChart(history, range, charts)}</div>
        <div class="algo-chart-legend"><span><i></i> Account equity</span><span>Times: ${escapeHtml(localTimeZone())}</span></div>
    </article>`;
}

function basketPanel(payload) {
    const basket = payload.basket || [];
    const rows = basket.length
        ? basket.map(row => `<li class="algo-activity-item"><span class="algo-run-version">${escapeHtml(row.symbol)}</span><span class="${classForReturn(row.momentum_6m)}">${escapeHtml(signedPercent(100 * row.momentum_6m))} 6-month</span><span>${escapeHtml(signedPercent(100 * row.weight, 0))}</span></li>`).join('')
        : '<li class="algo-muted-text">No basket recorded yet.</li>';
    const lastRebalance = payload.last_rebalance ? escapeHtml(dateTime(payload.last_rebalance)) : 'never';
    return `<article class="card algo-panel algo-strategy-panel">
        <div class="algo-section-heading"><div><span class="algo-eyebrow">MOMENTUM BASKET</span><h2>${escapeHtml(payload.latest_quarter || 'Not rebalanced yet')}</h2></div><span class="algo-state-pill"><i></i> ${payload.demo_mode ? 'DEMO' : payload.available ? 'PAPER' : 'UNAVAILABLE'}</span></div>
        <div class="algo-strategy-score"><div class="algo-score-ring"><span>${basket.length}</span><small>/names</small></div><div><strong>Top six-month momentum, equal weight</strong><span>Rebalanced quarterly</span></div></div>
        <div class="algo-strategy-list"><div><span>Last rebalance</span><strong>${lastRebalance}</strong></div><div><span>Orders in that quarter</span><strong>${escapeHtml(String(payload.quarter_orders ?? 0))}</strong></div><div><span>Next rebalance</span><strong>first week of next quarter, 10:00 NY</strong></div></div>
        <ol class="algo-activity-list">${rows}</ol>
    </article>`;
}

function positionsPanel(payload) {
    const rows = payload.positions.length
        ? payload.positions.map(row => `<tr><td>${escapeHtml(row.symbol)}</td><td>${escapeHtml(String(row.qty))}</td><td>${escapeHtml(money(row.avg_entry_price))}</td><td>${escapeHtml(money(row.current_price))}</td><td>${escapeHtml(money(row.market_value))}</td><td class="${classForReturn(row.unrealized_pl)}">${escapeHtml(signedMoney(row.unrealized_pl))} (${escapeHtml(signedPercent(100 * row.unrealized_plpc))})</td></tr>`).join('')
        : '<tr><td colspan="6" class="algo-muted-text">No open positions.</td></tr>';
    return `<section class="card algo-panel algo-positions-panel">
        <div class="algo-section-heading"><div><span class="algo-eyebrow">POSITIONS</span><h2>Open positions</h2><p>${payload.demo_mode ? 'Simulated holdings' : 'Paper account holdings'}, marked at the latest price.</p></div></div>
        <div class="table-scroll"><table class="algo-table"><thead><tr><th>Symbol</th><th>Qty</th><th>Avg entry</th><th>Price</th><th>Value</th><th>Unrealized P/L</th></tr></thead><tbody>${rows}</tbody></table></div>
    </section>`;
}

function activityPanel(payload) {
    const items = (payload.activity || []).slice(0, ACTIVITY_LIMIT);
    const rows = items.length
        ? items.map(item => `<li class="algo-activity-item"><span class="algo-activity-icon ${escapeHtml(item.kind)}" aria-hidden="true">${activityIcon(item.kind)}</span><div class="algo-activity-copy"><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(item.detail)}</span></div><time>${escapeHtml(dateOnly(item.at))} ${escapeHtml(timeOnly(item.at))}</time></li>`).join('')
        : '<li class="algo-muted-text">No orders on this account yet.</li>';
    return `<article class="card algo-panel algo-activity-panel">
        <div class="algo-section-heading"><div><span class="algo-eyebrow">ORDERS &amp; JOURNAL</span><h2>Latest activity</h2></div><span class="algo-feed-label"><i></i> ${payload.demo_mode ? 'DEMO' : 'PAPER'}</span></div>
        <ol class="algo-activity-list">${rows}</ol>
        <div class="algo-view-more"><span>${payload.demo_mode ? 'Example order activity only; no orders are sent' : 'Orders are read from the momentum paper account'}</span><span aria-hidden="true">↻</span></div>
    </article>`;
}

export function renderMomentumPage({ payload, range = '1M', charts, loading = false, error = '' }) {
    if (!payload) {
        return `<main class="algo-page"><header class="algo-page-header"><div><div class="algo-breadcrumb">SYSTEMS <span>/</span> STRATEGY MONITOR</div><h1>Algo trader</h1>${switchMarkup('momentum')}</div></header>
            <section class="card algo-connection-card"><div class="algo-loading-mark">↻</div><strong>${loading ? 'Loading the momentum paper account…' : 'Momentum account unavailable'}</strong><p>${escapeHtml(error || 'Checking the momentum paper account.')}</p></section></main>`;
    }
    const available = payload.available === true;
    const header = `<header class="algo-page-header">
        <div><div class="algo-breadcrumb">SYSTEMS <span>/</span> STRATEGY MONITOR</div><h1>Algo trader</h1>${switchMarkup('momentum')}</div>
        <div class="algo-header-status"><span class="algo-demo-tag"><i></i> ${payload.demo_mode ? 'SIMULATED MOMENTUM' : 'ALPACA MOMENTUM PAPER'}</span><span class="algo-state-pill"><i></i> QUARTERLY REBALANCE</span></div>
    </header>`;
    const demoNotice = payload.demo_mode
        ? '<section class="algo-demo-notice"><span class="algo-notice-mark">i</span><p><strong>Example data only.</strong> Account values, positions and activity are simulated. No broker is contacted and no orders are sent.</p></section>'
        : '';
    if (!available) {
        return `<main class="algo-page">${header}${unavailable(payload, error)}${basketPanel(payload)}${positionsPanel(payload)}${activityPanel(payload)}</main>`;
    }
    return `<main class="algo-page algo-momentum-page">
        ${header}${demoNotice}
        ${kpis(payload)}
        <section class="algo-primary-grid">
            ${chartPanel(payload, range, charts)}
            ${basketPanel(payload)}
        </section>
        <section class="algo-secondary-grid algo-secondary-grid-equity">
            ${positionsPanel(payload)}
            ${activityPanel(payload)}
        </section>
        <p class="algo-muted-text">Updated ${escapeHtml(dateTime(payload.generated_at))}</p>
    </main>`;
}
