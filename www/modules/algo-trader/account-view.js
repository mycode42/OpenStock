import { escapeHtml } from '../escape.js';
import { money, signedMoney, signedPercent, dateTime, timeOnly, dateOnly, classForReturn, statusClass, activityIcon } from './format.js';
import { RANGES, localTimeZone, renderChart, sparklinePath } from './equity-chart.js';
import { renderStrategyPerformance } from './strategies.js';

const ACTIVITY_PAGE_SIZE = 10;

export function renderAccountSwitch({ active, ariaLabel, attribute, options }) {
    return `<div class="algo-account-switch algo-title-account-switch" role="group" aria-label="${ariaLabel}">${options.map(([mode, label]) => `<button type="button" ${attribute}="${mode}" class="${active === mode ? 'active' : ''}" aria-pressed="${active === mode}">${label}</button>`).join('')}</div>`;
}

function renderAccountModeSwitch(active) {
    return renderAccountSwitch({ active, ariaLabel: 'Algo Trader paper account', attribute: 'data-account-mode', options: [['equity', 'Equity Account'], ['options', 'Options Account']] });
}

export function renderAccountLoading({ mode, loading, error }) {
    const isOptions = mode === 'options';
    return `<main class="algo-page"><header class="algo-page-header"><div><div class="algo-breadcrumb">SYSTEMS <span>/</span> STRATEGY MONITOR</div><h1>Algo trader</h1>${renderAccountModeSwitch(mode)}</div></header><section class="card algo-connection-card"><div class="algo-loading-mark">↻</div><strong>${loading ? `Connecting to ${isOptions ? 'Options' : 'Equity'} Account…` : `${isOptions ? 'Options' : 'Equity'} Account data unavailable`}</strong><p>${escapeHtml(error || 'Loading the paper-account snapshot and coordinator data.')}</p><button type="button" class="btn btn-secondary" data-action="retry">Retry connection</button></section></main>`;
}

export function renderAccountPage({ mode, snapshot, settings, range, activityPage, charts, renderConfig }) {
    const isOptions = mode === 'options';
    const account = snapshot.account || {};
    const demoMode = snapshot.demo_mode === true;
    const performance = snapshot.performance || {};
    const metrics = performance.metrics || {};
    const history = performance.history || [];
    const activities = snapshot.activity || [];
    const activityPageCount = Math.max(1, Math.ceil(activities.length / ACTIVITY_PAGE_SIZE));
    activityPage = Math.min(activityPage, activityPageCount - 1);
    const activityStart = activityPage * ACTIVITY_PAGE_SIZE;
    const visibleActivities = activities.slice(activityStart, activityStart + ACTIVITY_PAGE_SIZE);
    const latestSnapshot = history[history.length - 1];
    const snapshotDate = latestSnapshot?.date ? dateOnly(latestSnapshot.date) : 'no snapshots';
    const pauseReasons = Array.isArray(account.entry_pause_reasons) ? account.entry_pause_reasons : [];
    const marketStatus = String(account.market_status || account.market_state || '').toUpperCase();
    const regimeLabel = String(account.regime || 'unknown').replaceAll('_', ' ').toUpperCase();
    const scanStatus = String(account.last_scan_status || 'unknown').trim() || 'unknown';
    const coordinatorStatusLabel = demoMode ? 'DEMO' : ['ok', 'complete', 'completed', 'success', 'succeeded'].includes(scanStatus.toLowerCase())
        ? 'OK' : scanStatus.replaceAll('_', ' ').toUpperCase();
    const regimeEngineLabel = String(account.regime_engine || 'unknown').replaceAll('_', ' ').toUpperCase();
    const closedMarketQuotePause = marketStatus === 'CLOSED'
        && pauseReasons.length === 1
        && pauseReasons[0] === 'owned positions lack fresh protective quotes';
    const optionsMarketClosedPause = isOptions
        && marketStatus === 'CLOSED'
        && account.entries_paused === true
        && pauseReasons.length === 1
        && pauseReasons[0] === 'Options market is closed';
    const equityMarketClosedPause = !isOptions
        && marketStatus === 'CLOSED'
        && account.entries_paused === true
        && pauseReasons.length === 1
        && pauseReasons[0] === 'Equity market is closed';
    const marketClosedPause = closedMarketQuotePause || optionsMarketClosedPause || equityMarketClosedPause;
    const entryStatus = marketClosedPause ? 'MARKET CLOSED' : account.entries_paused === true ? 'ENTRIES PAUSED' : account.entries_paused === false ? 'ENTRIES ENABLED' : 'ENTRY STATUS UNKNOWN';
    const entryClass = marketClosedPause ? 'algo-state-pill market-closed' : account.entries_paused === true ? 'algo-state-pill paused' : account.entries_paused === false ? 'algo-state-pill' : 'algo-state-pill unknown';
    const pauseNoticeTitle = marketClosedPause ? 'Market closed' : 'New entries are paused';
    const pauseNoticeDetail = optionsMarketClosedPause
        ? 'The options marks and regime shown are from the last market session. New entries can resume when the market reopens.'
        : closedMarketQuotePause
        ? 'Protective quotes will be checked again when the market reopens. New entries remain paused until then.'
        : pauseReasons.join(' · ');
    const strategyListLabel = isOptions ? account.active_strategies_label || 'Strategies available in this regime' : 'Active strategies';
    const noStrategiesMessage = isOptions ? 'No enabled strategy sleeves for this regime' : 'No active strategies in the last scan';
    const strategyScope = isOptions
        ? `${regimeLabel} regime · ${regimeEngineLabel} engine`
        : `${account.universe_count ?? 0} tickers · ${regimeEngineLabel} regime engine`;

    const html = `<main class="algo-page">
        <header class="algo-page-header">
            <div><div class="algo-breadcrumb">SYSTEMS <span>/</span> STRATEGY MONITOR</div><h1>Algo trader</h1>${renderAccountModeSwitch(mode)}</div>
            <div class="algo-header-status"><span class="algo-demo-tag"><i></i> ${demoMode ? 'SIMULATED DEMO' : `ALPACA ${isOptions ? 'OPTIONS ' : ''}PAPER`}</span><span class="${entryClass}"><i></i> ${entryStatus}</span></div>
        </header>

        ${demoMode ? '<section class="algo-demo-notice"><span class="algo-notice-mark">i</span><p><strong>Example data only.</strong> Prices, account values, signals and activity below are simulated. No strategy is running and no orders are sent.</p></section>' : ''}
        ${account.entries_paused && pauseReasons.length ? `<section class="algo-paused-notice ${marketClosedPause ? 'market-closed' : ''}"><strong>${pauseNoticeTitle}</strong><span>${escapeHtml(pauseNoticeDetail)}</span></section>` : ''}

        <section class="algo-kpi-grid" aria-label="Algo account KPIs">
            <article class="algo-kpi algo-kpi-featured"><div class="algo-kpi-label">ACCOUNT EQUITY <span class="algo-kpi-period">${escapeHtml(snapshotDate)}</span></div><strong>${money(metrics.equity)}</strong><div class="algo-kpi-foot"><span>${isOptions ? 'Open positions' : 'Cash'} ${isOptions ? latestSnapshot?.position_count ?? '—' : money(latestSnapshot?.cash)}</span><span>${isOptions ? 'Options equity' : 'Invested'} ${isOptions ? money(metrics.equity) : money(latestSnapshot?.invested)}</span></div><div class="algo-kpi-spark"><svg viewBox="0 0 120 32" aria-hidden="true"><path d="${sparklinePath(history)}"/></svg></div></article>
            <article class="algo-kpi"><div class="algo-kpi-label">EQUITY CHANGE</div><strong class="${classForReturn(metrics.total_return_pct)}">${signedPercent(metrics.total_return_pct)}</strong><div class="algo-kpi-foot"><span class="${classForReturn(metrics.total_pnl)}">${signedMoney(metrics.total_pnl)}</span><span>since first snapshot</span></div></article>
            <article class="algo-kpi"><div class="algo-kpi-label">LATEST DAILY P&amp;L</div><strong class="${classForReturn(metrics.daily_pnl)}">${signedMoney(metrics.daily_pnl)}</strong><div class="algo-kpi-foot"><span class="${classForReturn(metrics.daily_pnl_pct)}">${signedPercent(metrics.daily_pnl_pct, 3)}</span><span>last account snapshot</span></div></article>
            <article class="algo-kpi"><div class="algo-kpi-label">MAX DRAWDOWN</div><strong class="algo-kpi-negative">${metrics.max_drawdown_pct === null ? '—' : `${metrics.max_drawdown_pct.toFixed(2)}%`}</strong><div class="algo-kpi-foot"><span>snapshot series</span><span>${history.length} points</span></div></article>
            <article class="algo-kpi"><div class="algo-kpi-label">OPEN POSITIONS</div><strong>${latestSnapshot?.position_count ?? '—'}</strong><div class="algo-kpi-foot"><span>Active strategies</span><span>${account.active_strategies?.length ?? 0}</span></div></article>
        </section>

        <section class="algo-primary-grid">
            <article class="card algo-panel algo-chart-panel" data-chart-readout>
                <div class="algo-section-heading algo-chart-heading"><div><span class="algo-eyebrow">ACCOUNT PERFORMANCE</span><h2>Equity history</h2><p>Recorded paper-account equity · USD</p></div><div class="algo-chart-summary"><strong data-chart-value data-default-chart-value="${money(metrics.equity)}">${money(metrics.equity)}</strong><span class="${classForReturn(metrics.total_return_pct)}">${signedPercent(metrics.total_return_pct)} since first snapshot</span></div></div>
                <div class="algo-range-controls" role="group" aria-label="Chart time range">${RANGES.map(value => `<button type="button" class="algo-range-btn ${range === value ? 'active' : ''}" data-range="${value}" aria-pressed="${range === value}">${value}</button>`).join('')}</div>
                <div class="algo-chart-wrap">${renderChart(history, range, charts)}</div>
                <div class="algo-chart-legend"><span><i></i> Account equity</span><span>Times: ${escapeHtml(localTimeZone())}</span></div>
            </article>

            <article class="card algo-panel algo-strategy-panel">
                <div class="algo-section-heading"><div><span class="algo-eyebrow">COORDINATOR</span><h2>${escapeHtml(regimeLabel)}</h2></div><span class="algo-state-pill"><i></i> ${escapeHtml(coordinatorStatusLabel)}</span></div>
                <div class="algo-strategy-score"><div class="algo-score-ring"><span>${account.active_strategies?.length ?? 0}</span><small>/strats</small></div><div><strong>${escapeHtml(strategyListLabel)}</strong><span>${isOptions ? escapeHtml(strategyScope) : strategyScope}</span></div></div>
                <div class="algo-active-strategy-list">${(account.active_strategies || []).map(strategy => `<span>${escapeHtml(strategy)}</span>`).join('') || `<span class="algo-muted-text">${escapeHtml(noStrategiesMessage)}</span>`}</div>
                <div class="algo-strategy-list"><div><span>Last scan</span><strong>${escapeHtml(dateTime(account.last_scan_at))}</strong></div><div><span>Account mode</span><strong>${demoMode ? 'Simulated demo' : `Alpaca ${isOptions ? 'options ' : ''}paper`}</strong></div><div><span>Sharpe ratio</span><strong>${metrics.sharpe_ratio === null ? '—' : metrics.sharpe_ratio.toFixed(2)} <small>${demoMode ? 'sample series' : 'snapshot based'}</small></strong></div></div>
                ${demoMode ? '' : `<button type="button" class="algo-text-link" data-action="open-config">Manage ${isOptions ? 'options' : 'runtime'} settings <span>↗</span></button>`}
            </article>
        </section>

        ${renderStrategyPerformance(snapshot.strategy_performance, isOptions, settings, demoMode)}

        ${demoMode ? '<section class="algo-demo-notice"><span class="algo-notice-mark">i</span><p><strong>Setup required.</strong> Connect Alpaca and the Algo Trader service to load real account history and edit strategy settings.</p></section>' : `<section class="card algo-panel algo-config-panel" id="algo-config">
            <div class="algo-section-heading algo-config-heading"><div><span class="algo-eyebrow">${isOptions ? 'OPTIONS CONFIGURATION' : 'RUNTIME CONFIGURATION'}</span><h2>${isOptions ? 'Options strategy settings' : 'Algo strategy settings'}</h2><p>${isOptions ? 'Independent settings for the options paper account and research engine.' : 'These settings are read by the Algo coordinator and autoresearch runtime.'}</p></div><span class="algo-local-save">${isOptions ? 'OPTIONS CONFIG' : 'CONNECTED CONFIG'}</span></div>
            ${renderConfig()}
        </section>`}

        <section class="algo-secondary-grid algo-secondary-grid-equity">
            <article class="card algo-panel algo-activity-panel">
                <div class="algo-section-heading"><div><span class="algo-eyebrow">${isOptions ? 'OPTIONS ENGINE &amp; JOURNAL' : 'COORDINATOR &amp; JOURNAL'}</span><h2>Latest activity</h2></div><span class="algo-feed-label"><i></i> ${demoMode ? 'SIMULATED' : 'REFRESHING'}</span></div>
                ${activities.length ? `<ol class="algo-activity-list">${visibleActivities.map(item => `<li class="algo-activity-item"><span class="algo-activity-icon ${escapeHtml(item.kind)}" aria-hidden="true">${activityIcon(item.kind)}</span><div class="algo-activity-content"><div class="algo-activity-title"><strong>${escapeHtml(item.title)}</strong><time>${escapeHtml(timeOnly(item.at))}</time></div><p>${escapeHtml(item.detail || '')}</p><span class="algo-activity-tag ${statusClass(item.status)}">${escapeHtml(item.status || item.kind)}</span></div></li>`).join('')}</ol><div class="journal-pagination algo-activity-pagination"><span>Showing ${activityStart + 1}–${Math.min(activityStart + visibleActivities.length, activities.length)} of ${activities.length} entries · ${ACTIVITY_PAGE_SIZE} per page</span>${activityPageCount > 1 ? `<div class="journal-pagination-actions"><button type="button" class="btn btn-secondary" data-activity-page="${activityPage - 1}" ${activityPage === 0 ? 'disabled' : ''} aria-label="Previous activity page">Previous</button><span>Page ${activityPage + 1} of ${activityPageCount}</span><button type="button" class="btn btn-secondary" data-activity-page="${activityPage + 1}" ${activityPage >= activityPageCount - 1 ? 'disabled' : ''} aria-label="Next activity page">Next</button></div>` : ''}</div>` : '<div class="algo-empty">No recent scans, signals or order journal entries are available.</div>'}
                <div class="algo-view-more"><span>${demoMode ? 'All entries shown here are simulated' : 'Entries are read from the Algo state and trade journal'}</span><span aria-hidden="true">↻</span></div>
            </article>
        </section>

    </main>`;
    return { html, activityPage };
}
