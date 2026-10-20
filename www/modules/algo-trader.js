import { escapeHtml } from './escape.js';

const RANGES = ['1W', '1M', '3M', 'ALL'];
const SETTING_FIELDS = [
    { key: 'overnight_entries_enabled', title: 'Overnight entries', detail: 'Allow new positions during overnight sessions.' },
    { key: 'enable_intraday_trend', title: 'Intraday trend', detail: 'Include the intraday trend strategy in scans.' },
    { key: 'enable_intraday_momentum', title: 'Intraday momentum', detail: 'Include the intraday momentum strategy in scans.' },
    { key: 'algo_multi_factor_enabled', title: 'Multi Factor', detail: 'Include the Multi Factor strategy in the active portfolio.' },
    { key: 'algo_event_driven_enabled', title: 'Event driven', detail: 'Allow event driven signals in the active portfolio.' },
];

function numberOrNull(value) {
    const number = Number(value);
    return value !== null && value !== undefined && value !== '' && Number.isFinite(number) ? number : null;
}

function money(value, currency = 'USD', digits = 2) {
    const number = numberOrNull(value);
    if (number === null) return '—';
    return new Intl.NumberFormat(undefined, { style: 'currency', currency, minimumFractionDigits: digits, maximumFractionDigits: digits }).format(number);
}

function signedMoney(value, currency = 'USD') {
    const number = numberOrNull(value);
    if (number === null) return '—';
    return `${number > 0 ? '+' : ''}${money(number, currency)}`;
}

function signedPercent(value, digits = 2) {
    const number = numberOrNull(value);
    if (number === null) return '—';
    return `${number > 0 ? '+' : ''}${number.toFixed(digits)}%`;
}

function dateTime(value, options = {}) {
    if (!value) return '—';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short', ...options }).format(date);
}

function timeOnly(value) {
    if (!value) return '';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '' : new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit' }).format(date);
}

function dateOnly(value) {
    if (!value) return '';
    const date = new Date(`${String(value).slice(0, 10)}T12:00:00`);
    return Number.isNaN(date.getTime()) ? String(value) : new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' }).format(date);
}

function classForReturn(value) {
    const number = numberOrNull(value);
    return number === null || number === 0 ? '' : number > 0 ? 'up' : 'down';
}

function rangeHistory(history, range) {
    const points = Array.isArray(history) ? history.filter(item => numberOrNull(item.equity) !== null) : [];
    if (range === 'ALL' || points.length <= 1) return points;
    const lookback = range === '1W' ? 7 : range === '1M' ? 31 : 92;
    const latestDate = new Date(points[points.length - 1].timestamp || points[points.length - 1].date);
    if (Number.isNaN(latestDate.getTime())) return points;
    const cutoff = latestDate.getTime() - lookback * 86400000;
    const filtered = points.filter(item => new Date(item.timestamp || item.date).getTime() >= cutoff);
    return filtered.length > 1 ? filtered : points;
}

function renderChart(history, range) {
    const points = rangeHistory(history, range);
    if (points.length < 2) return '<div class="algo-chart-empty">Equity history will appear after more account snapshots are recorded.</div>';
    const width = 800;
    const height = 270;
    const left = 74;
    const right = 784;
    const top = 20;
    const bottom = 220;
    const values = points.map(point => Number(point.equity));
    const low = Math.min(...values);
    const high = Math.max(...values);
    const padding = Math.max((high - low) * 0.16, Math.max(Math.abs(high), 1) * 0.0005);
    const min = low - padding;
    const max = high + padding;
    const coords = values.map((value, index) => ({
        x: left + (right - left) * (index / (values.length - 1)),
        y: bottom - ((value - min) / (max - min || 1)) * (bottom - top),
        value,
    }));
    const line = coords.map((point, index) => `${index ? 'L' : 'M'} ${point.x.toFixed(1)} ${point.y.toFixed(1)}`).join(' ');
    const area = `${line} L ${right} ${bottom} L ${left} ${bottom} Z`;
    const grid = [0, 1, 2, 3].map(index => {
        const y = top + (bottom - top) * (index / 3);
        const value = max - (max - min) * (index / 3);
        return `<line class="algo-chart-grid" x1="${left}" y1="${y}" x2="${right}" y2="${y}"/><text class="algo-chart-y-label" x="${left - 12}" y="${y + 4}" text-anchor="end">${escapeHtml(money(value, 'USD', 0))}</text>`;
    }).join('');
    const labelIndexes = [...new Set([0, Math.floor((points.length - 1) / 2), points.length - 1])];
    const labels = labelIndexes.map(index => {
        const anchor = index === 0 ? 'start' : index === points.length - 1 ? 'end' : 'middle';
        return `<text class="algo-chart-x-label" x="${coords[index].x}" y="252" text-anchor="${anchor}">${escapeHtml(dateOnly(points[index].date || points[index].timestamp))}</text>`;
    }).join('');
    const circles = coords.map((point, index) => index === coords.length - 1 ? `<circle class="algo-chart-point" cx="${point.x}" cy="${point.y}" r="4"/>` : '').join('');
    return `<svg class="algo-performance-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="Algo paper account equity history for ${escapeHtml(range)}">
        <defs><linearGradient id="algo-chart-fill" x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stop-color="#c8ff00" stop-opacity=".2"/><stop offset="100%" stop-color="#c8ff00" stop-opacity="0"/></linearGradient></defs>
        ${grid}<path class="algo-chart-area" d="${area}"/><path class="algo-chart-line" d="${line}"/>${circles}${labels}
    </svg>`;
}

function statusClass(value) {
    const status = String(value || '').toLowerCase();
    if (['filled', 'complete', 'ok', 'promoted'].includes(status)) return 'promising';
    if (['failed', 'error', 'rejected', 'cancelled', 'canceled'].includes(status)) return 'rejected';
    if (['staged'].includes(status)) return 'staged';
    return '';
}

function activityIcon(kind) {
    return ({ order: '⇄', scan: '◷', signal: '↗', research: '⌕' })[kind] || '·';
}

export function createAlgoTrader({ request }) {
    const host = document.getElementById('page-algo-trader');
    let snapshot = null;
    let errorMessage = '';
    let loading = false;
    let saving = false;
    let dirty = false;
    let draftSettings = null;
    let range = '1M';
    let researchFilter = 'all';
    let selectedRunDate = null;
    let notice = '';
    let pollTimer = null;
    let requestInFlight = false;

    function visibleRuns() {
        const history = snapshot?.research?.history || [];
        if (researchFilter === 'promoted') return history.filter(run => run.promoted > 0);
        if (researchFilter === 'no-promotion') return history.filter(run => !run.promoted);
        return history;
    }

    function renderRunDetail(run) {
        if (!run) return '<div class="algo-empty">No autoresearch reports are available yet.</div>';
        return `<div class="algo-research-detail-head"><div><span class="algo-eyebrow">RESEARCH RUN</span><h3>${escapeHtml(run.date || 'Undated run')}</h3></div><span class="algo-status-pill ${statusClass(run.status)}">${escapeHtml(run.status || 'unknown')}</span></div>
            <p class="algo-research-note">${escapeHtml(run.result || 'Research result unavailable')}${run.portfolio_status && run.portfolio_status !== 'unavailable' ? ` · portfolio replay ${escapeHtml(run.portfolio_status)}` : ''}</p>
            ${run.factor_model ? `<div class="algo-run-metrics"><div><span>Factor model</span><strong>${escapeHtml(run.factor_model.family)}</strong></div><div><span>Observations</span><strong>${run.factor_model.observations ?? '—'}</strong></div><div><span>Rolling evaluations</span><strong>${run.factor_model.rolling_evaluations ?? '—'}</strong></div><div><span>Forward horizon</span><strong>${run.factor_model.forward_horizon_sessions ?? '—'} sessions</strong></div></div><p class="algo-research-note">Discovery ${escapeHtml(run.discovery_status)}${run.discovery_reason ? ` · ${escapeHtml(run.discovery_reason)}` : ''}</p>` : `<div class="algo-run-metrics"><div><span>Active trials</span><strong>${run.active_trials ?? '—'}</strong></div><div><span>Promoted</span><strong>${run.promoted ?? 0}</strong></div><div><span>Staged</span><strong>${run.staged ?? 0}</strong></div><div><span>Portfolio return</span><strong class="${classForReturn(run.portfolio_return_pct)}">${signedPercent(run.portfolio_return_pct)}</strong></div></div>`}
            <small class="algo-detail-time">${run.created_at ? `Completed ${escapeHtml(dateTime(run.created_at))}` : ''}${run.duration_seconds !== null && run.duration_seconds !== undefined ? ` · ${Math.round(run.duration_seconds / 60)} min` : ''}</small>`;
    }

    function renderSettings(settings) {
        const toggles = SETTING_FIELDS.map(field => `<label class="algo-config-toggle"><input type="checkbox" name="${field.key}" ${settings[field.key] ? 'checked' : ''}><span><strong>${escapeHtml(field.title)}</strong><small>${escapeHtml(field.detail)}</small></span><i></i></label>`).join('');
        const sizePct = (Number(settings.intraday_satellite_position_pct) * 100).toFixed(2).replace(/0+$/, '').replace(/\.$/, '');
        return `<form id="algo-config-form" class="algo-config-form">
            <div class="algo-config-toggles">${toggles}</div>
            <div class="algo-config-field"><label for="algo-satellite-size">Intraday satellite size <small>% of equity</small></label><input id="algo-satellite-size" type="number" name="intraday_satellite_position_pct" min="0.1" max="4" step="0.1" value="${escapeHtml(sizePct)}"><small class="algo-field-help">Configured cap for each satellite position.</small></div>
            <div class="algo-config-field"><label for="algo-satellite-risk">Satellite risk multiplier</label><input id="algo-satellite-risk" type="number" name="intraday_satellite_risk_multiplier" min="0.05" max="1" step="0.05" value="${escapeHtml(settings.intraday_satellite_risk_multiplier)}"><small class="algo-field-help">Scales risk for intraday satellite strategies.</small></div>
            <div class="algo-config-actions"><p class="algo-save-notice" aria-live="polite">${escapeHtml(notice || 'Changes apply during the next portfolio scan.')}</p><button type="submit" class="btn btn-primary algo-save-button" ${saving ? 'disabled' : ''}>${saving ? 'Saving…' : 'Save runtime settings'} <span>→</span></button></div>
        </form>`;
    }

    function render() {
        if (!host) return;
        if (!snapshot) {
            host.innerHTML = `<main class="algo-page"><header class="algo-page-header"><div><div class="algo-breadcrumb">SYSTEMS <span>/</span> STRATEGY MONITOR</div><h1>Algo trader</h1><p class="subtitle">Strategy performance, activity and research in one place.</p></div></header><section class="card algo-connection-card"><div class="algo-loading-mark">↻</div><strong>${loading ? 'Connecting to Algo Trader…' : 'Algo Trader data unavailable'}</strong><p>${escapeHtml(errorMessage || 'Loading the paper-account snapshot and research records.')}</p><button type="button" class="btn btn-secondary" data-action="retry">Retry connection</button></section></main>`;
            return;
        }

        const account = snapshot.account || {};
        const performance = snapshot.performance || {};
        const metrics = performance.metrics || {};
        const history = performance.history || [];
        const settings = draftSettings || snapshot.settings || {};
        const research = snapshot.research || { latest: null, history: [] };
        const latestResearch = research.latest;
        const runs = visibleRuns();
        if (!runs.some(run => run.date === selectedRunDate)) selectedRunDate = runs[0]?.date || null;
        const selectedRun = runs.find(run => run.date === selectedRunDate) || null;
        const activities = snapshot.activity || [];
        const latestSnapshot = history[history.length - 1];
        const snapshotDate = latestSnapshot?.date ? dateOnly(latestSnapshot.date) : 'no snapshots';
        const entryStatus = account.entries_paused === true ? 'ENTRIES PAUSED' : account.entries_paused === false ? 'ENTRIES ENABLED' : 'ENTRY STATUS UNKNOWN';
        const entryClass = account.entries_paused === true ? 'algo-state-pill paused' : account.entries_paused === false ? 'algo-state-pill' : 'algo-state-pill unknown';

        host.innerHTML = `<main class="algo-page">
            <header class="algo-page-header">
                <div><div class="algo-breadcrumb">SYSTEMS <span>/</span> STRATEGY MONITOR</div><h1>Algo trader</h1><p class="subtitle">Paper-account performance, coordinator activity and autoresearch.</p></div>
                <div class="algo-header-status"><span class="algo-demo-tag"><i></i> ALPACA PAPER</span><span class="${entryClass}"><i></i> ${entryStatus}</span></div>
            </header>

            <section class="algo-demo-notice algo-live-notice"><span class="algo-notice-mark">↻</span><p><strong>Connected to /opt/algo-trader</strong> · Account and research data come from persisted Algo reports. Equity snapshot: ${escapeHtml(snapshotDate)}${account.last_scan_at ? ` · Last portfolio scan: ${escapeHtml(dateTime(account.last_scan_at))}` : ''}. Dashboard refreshes every 15 seconds.</p></section>
            ${account.entries_paused && account.entry_pause_reasons?.length ? `<section class="algo-paused-notice"><strong>New entries are paused</strong><span>${escapeHtml(account.entry_pause_reasons.join(' · '))}</span></section>` : ''}

            <section class="algo-kpi-grid" aria-label="Algo account KPIs">
                <article class="algo-kpi algo-kpi-featured"><div class="algo-kpi-label">ACCOUNT EQUITY <span class="algo-kpi-period">${escapeHtml(snapshotDate)}</span></div><strong>${money(metrics.equity)}</strong><div class="algo-kpi-foot"><span>Cash ${money(latestSnapshot?.cash)}</span><span>Invested ${money(latestSnapshot?.invested)}</span></div><div class="algo-kpi-spark"><svg viewBox="0 0 120 32" aria-hidden="true"><path d="${sparklinePath(history)}"/></svg></div></article>
                <article class="algo-kpi"><div class="algo-kpi-label">EQUITY CHANGE</div><strong class="${classForReturn(metrics.total_return_pct)}">${signedPercent(metrics.total_return_pct)}</strong><div class="algo-kpi-foot"><span class="${classForReturn(metrics.total_pnl)}">${signedMoney(metrics.total_pnl)}</span><span>since first snapshot</span></div></article>
                <article class="algo-kpi"><div class="algo-kpi-label">LATEST DAILY P&amp;L</div><strong class="${classForReturn(metrics.daily_pnl)}">${signedMoney(metrics.daily_pnl)}</strong><div class="algo-kpi-foot"><span class="${classForReturn(metrics.daily_pnl_pct)}">${signedPercent(metrics.daily_pnl_pct, 3)}</span><span>last account snapshot</span></div></article>
                <article class="algo-kpi"><div class="algo-kpi-label">MAX DRAWDOWN</div><strong class="algo-kpi-negative">${metrics.max_drawdown_pct === null ? '—' : `${metrics.max_drawdown_pct.toFixed(2)}%`}</strong><div class="algo-kpi-foot"><span>snapshot series</span><span>${history.length} points</span></div></article>
                <article class="algo-kpi"><div class="algo-kpi-label">OPEN POSITIONS</div><strong>${latestSnapshot?.position_count ?? '—'}</strong><div class="algo-kpi-foot"><span>Active strategies</span><span>${account.active_strategies?.length ?? 0}</span></div></article>
            </section>

            <section class="algo-primary-grid">
                <article class="card algo-panel algo-chart-panel">
                    <div class="algo-section-heading algo-chart-heading"><div><span class="algo-eyebrow">ACCOUNT PERFORMANCE</span><h2>Equity history</h2><p>Recorded paper-account equity · USD</p></div><div class="algo-chart-summary"><strong>${money(metrics.equity)}</strong><span class="${classForReturn(metrics.total_return_pct)}">${signedPercent(metrics.total_return_pct)} since first snapshot</span></div></div>
                    <div class="algo-range-controls" role="group" aria-label="Chart time range">${RANGES.map(value => `<button type="button" class="algo-range-btn ${range === value ? 'active' : ''}" data-range="${value}" aria-pressed="${range === value}">${value}</button>`).join('')}</div>
                    <div class="algo-chart-wrap">${renderChart(history, range)}</div>
                    <div class="algo-chart-legend"><span><i></i> Account equity</span><span>Source: Algo daily snapshots</span></div>
                </article>

                <article class="card algo-panel algo-strategy-panel">
                    <div class="algo-section-heading"><div><span class="algo-eyebrow">COORDINATOR</span><h2>${escapeHtml(account.market_state || 'State unavailable')}</h2></div><span class="algo-state-pill ${account.entries_paused ? 'paused' : ''}"><i></i> ${escapeHtml(account.entries_paused ? 'PAUSED' : account.last_scan_status || 'UNKNOWN')}</span></div>
                    <div class="algo-strategy-score"><div class="algo-score-ring"><span>${account.active_strategies?.length ?? 0}</span><small>/strats</small></div><div><strong>Active strategies</strong><span>${account.universe_count ?? 0} tickers in configured universe</span></div></div>
                    <div class="algo-active-strategy-list">${(account.active_strategies || []).map(strategy => `<span>${escapeHtml(strategy)}</span>`).join('') || '<span class="algo-muted-text">No active strategies in the last scan</span>'}</div>
                    <div class="algo-strategy-list"><div><span>Last scan</span><strong>${escapeHtml(dateTime(account.last_scan_at))}</strong></div><div><span>Account mode</span><strong>Alpaca paper</strong></div><div><span>Sharpe ratio</span><strong>${metrics.sharpe_ratio === null ? '—' : metrics.sharpe_ratio.toFixed(2)} <small>snapshot based</small></strong></div></div>
                    <button type="button" class="algo-text-link" data-action="open-config">Manage runtime settings <span>↗</span></button>
                </article>
            </section>

            <section class="algo-secondary-grid">
                <article class="card algo-panel algo-activity-panel">
                    <div class="algo-section-heading"><div><span class="algo-eyebrow">COORDINATOR &amp; JOURNAL</span><h2>Latest activity</h2></div><span class="algo-feed-label"><i></i> REFRESHING</span></div>
                    ${activities.length ? `<ol class="algo-activity-list">${activities.map(item => `<li class="algo-activity-item"><span class="algo-activity-icon ${escapeHtml(item.kind)}" aria-hidden="true">${activityIcon(item.kind)}</span><div class="algo-activity-content"><div class="algo-activity-title"><strong>${escapeHtml(item.title)}</strong><time>${escapeHtml(timeOnly(item.at))}</time></div><p>${escapeHtml(item.detail || '')}</p><span class="algo-activity-tag ${statusClass(item.status)}">${escapeHtml(item.status || item.kind)}</span></div></li>`).join('')}</ol>` : '<div class="algo-empty">No recent scans, signals or order journal entries are available.</div>'}
                    <div class="algo-view-more"><span>Entries are read from the Algo state and trade journal</span><span aria-hidden="true">↻</span></div>
                </article>

                <article class="card algo-panel algo-research-panel">
                    <div class="algo-section-heading"><div><span class="algo-eyebrow">AUTORESEARCH</span><h2>Latest result</h2></div><span class="algo-status-pill ${statusClass(latestResearch?.status)}">${escapeHtml(latestResearch?.status || 'NO REPORT')}</span></div>
                    ${latestResearch ? `<div class="algo-latest-run"><div class="algo-run-icon">AR</div><div><strong>${escapeHtml(latestResearch.result || 'Research report')}</strong><span>${escapeHtml(latestResearch.date || 'Undated')} · ${latestResearch.created_at ? `completed ${escapeHtml(dateTime(latestResearch.created_at))}` : 'completion time unavailable'}</span></div></div>
                        <div class="algo-latest-result">${latestResearch.factor_model ? `<div><span>Observations</span><strong>${latestResearch.factor_model.observations ?? '—'}</strong></div><div><span>Rolling evals</span><strong>${latestResearch.factor_model.rolling_evaluations ?? '—'}</strong></div><div><span>Training sample</span><strong>${latestResearch.factor_model.training_samples ?? '—'}</strong></div>` : `<div><span>Promoted</span><strong>${latestResearch.promoted ?? 0}</strong></div><div><span>Staged</span><strong>${latestResearch.staged ?? 0}</strong></div><div><span>Active trials</span><strong>${latestResearch.active_trials ?? '—'}</strong></div>`}</div>
                        <div class="algo-research-recommendation"><span class="algo-recommendation-mark">i</span><p><strong>${escapeHtml(latestResearch.factor_model ? `${latestResearch.factor_model.family} · factor-only` : latestResearch.portfolio_status === 'skipped' ? 'Portfolio replay skipped' : latestResearch.portfolio_status || 'Research status')}</strong><br>${latestResearch.factor_model ? `Discovery ${escapeHtml(latestResearch.discovery_status)}${latestResearch.discovery_reason ? ` · ${escapeHtml(latestResearch.discovery_reason)}` : ''}.` : latestResearch.portfolio_return_pct === null ? 'No portfolio return was reported for this run.' : `Reported portfolio return: ${signedPercent(latestResearch.portfolio_return_pct)}.`}</p></div>`
                        : '<div class="algo-empty">No autoresearch reports are available in the Algo data directory.</div>'}
                    <button type="button" class="btn btn-secondary algo-open-history" data-action="open-history">Browse research history <span>→</span></button>
                </article>
            </section>

            <section class="card algo-panel algo-history-panel" id="algo-history">
                <div class="algo-section-heading algo-history-heading"><div><span class="algo-eyebrow">EXPERIMENT ARCHIVE</span><h2>Autoresearch history</h2><p>Daily saved research reports from /opt/algo-trader/data/research.</p></div><div class="algo-history-filters" role="group" aria-label="Filter research runs">${[['all', 'All runs'], ['promoted', 'Promoted'], ['no-promotion', 'No promotion']].map(([key, label]) => `<button type="button" data-research-filter="${key}" class="${researchFilter === key ? 'active' : ''}" aria-pressed="${researchFilter === key}">${label}</button>`).join('')}</div></div>
                <div class="algo-history-layout"><div class="algo-run-list" role="list">${runs.map(run => `<button type="button" role="listitem" class="algo-run-row ${run.date === selectedRunDate ? 'selected' : ''}" data-run-date="${escapeHtml(run.date)}" aria-pressed="${run.date === selectedRunDate}"><span class="algo-run-version">${escapeHtml(run.date || 'Undated')}<small>${run.created_at ? escapeHtml(timeOnly(run.created_at)) : 'saved report'}</small></span><span class="algo-run-name">${escapeHtml(run.result || 'Research report')}<small>${escapeHtml(run.portfolio_status || 'portfolio result unavailable')} · ${run.active_trials ?? '—'} active trials</small></span><span class="algo-run-return ${classForReturn(run.portfolio_return_pct)}">${signedPercent(run.portfolio_return_pct)}<small>portfolio return</small></span><span class="algo-status-pill ${statusClass(run.status)}">${escapeHtml(run.status || 'unknown')}</span><span class="algo-run-chevron">›</span></button>`).join('') || '<div class="algo-empty">No reports match this filter.</div>'}</div>
                    <aside class="algo-run-detail">${renderRunDetail(selectedRun)}</aside></div>
            </section>

            <section class="card algo-panel algo-config-panel" id="algo-config">
                <div class="algo-section-heading algo-config-heading"><div><span class="algo-eyebrow">RUNTIME CONFIGURATION</span><h2>Algo strategy settings</h2><p>These settings are read by the Algo coordinator and autoresearch runtime.</p></div><span class="algo-local-save">CONNECTED CONFIG</span></div>
                ${renderSettings(settings)}
            </section>
        </main>`;
    }

    function sparklinePath(history) {
        const values = (history || []).slice(-14).map(point => Number(point.equity)).filter(Number.isFinite);
        if (values.length < 2) return 'M1 27 L119 27';
        const min = Math.min(...values);
        const max = Math.max(...values);
        return values.map((value, index) => {
            const x = 1 + 118 * index / (values.length - 1);
            const y = 28 - ((value - min) / (max - min || 1)) * 24;
            return `${index ? 'L' : 'M'}${x.toFixed(1)} ${y.toFixed(1)}`;
        }).join(' ');
    }

    function readSettingsFromForm(form) {
        const settings = {};
        for (const field of SETTING_FIELDS) settings[field.key] = form.elements[field.key].checked;
        settings.intraday_satellite_position_pct = Number(form.elements.intraday_satellite_position_pct.value) / 100;
        settings.intraday_satellite_risk_multiplier = Number(form.elements.intraday_satellite_risk_multiplier.value);
        return settings;
    }

    async function load({ silent = false } = {}) {
        if (requestInFlight || document.hidden) return;
        requestInFlight = true;
        loading = !snapshot && !silent;
        if (loading) render();
        const data = await request('/algo-trader', {}, () => {});
        requestInFlight = false;
        loading = false;
        if (!data) {
            if (!snapshot) errorMessage = 'Check that OpenStock can read the Algo Trader data directory.';
            render();
            return;
        }
        snapshot = data;
        errorMessage = '';
        if (!dirty) draftSettings = null;
        render();
    }

    function startPolling() {
        if (pollTimer) return;
        pollTimer = window.setInterval(() => load({ silent: true }), 15000);
    }

    function stopPolling() {
        if (pollTimer) window.clearInterval(pollTimer);
        pollTimer = null;
    }

    host?.addEventListener('input', event => {
        if (event.target.closest('#algo-config-form')) {
            draftSettings = readSettingsFromForm(event.target.form);
            dirty = true;
        }
    });
    host?.addEventListener('change', event => {
        if (event.target.closest('#algo-config-form')) {
            draftSettings = readSettingsFromForm(event.target.form);
            dirty = true;
        }
    });
    host?.addEventListener('click', event => {
        const button = event.target.closest('button');
        if (!button || !host.contains(button)) return;
        if (button.dataset.range) {
            range = button.dataset.range;
            render();
        } else if (button.dataset.researchFilter) {
            researchFilter = button.dataset.researchFilter;
            selectedRunDate = null;
            render();
        } else if (button.dataset.runDate) {
            selectedRunDate = button.dataset.runDate;
            render();
        } else if (button.dataset.action === 'open-history') {
            document.getElementById('algo-history')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
        } else if (button.dataset.action === 'open-config') {
            document.getElementById('algo-config')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
        } else if (button.dataset.action === 'retry') {
            errorMessage = '';
            load();
        }
    });
    host?.addEventListener('submit', async event => {
        if (event.target.id !== 'algo-config-form') return;
        event.preventDefault();
        if (saving) return;
        const form = event.target;
        draftSettings = readSettingsFromForm(form);
        dirty = true;
        saving = true;
        notice = 'Saving settings to Algo Trader…';
        render();
        const result = await request('/algo-trader/config', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ settings: draftSettings }) }, () => {});
        saving = false;
        if (!result?.settings) {
            notice = 'Save failed. Check the values and retry.';
            render();
            return;
        }
        snapshot.settings = result.settings;
        draftSettings = null;
        dirty = false;
        notice = `Saved · applies on the ${result.applied || 'next portfolio scan'}.`;
        render();
    });
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden && pollTimer) load({ silent: true });
    });

    return { load, render, startPolling, stopPolling };
}
