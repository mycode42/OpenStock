import { escapeHtml } from '../escape.js';
import { signedPercent, signedScore, dateTime, timeOnly, classForReturn, statusClass } from './format.js';
import { renderAccountSwitch } from './account-view.js';

function renderRunDetail(run) {
    if (!run) return '<div class="algo-empty">No autoresearch reports are available yet.</div>';
    if (Array.isArray(run.options_strategies)) {
        const approved = run.options_strategies.filter(strategy => strategy.approved).length;
        const completed = run.options_strategies.filter(strategy => strategy.status === 'completed').length;
        const errors = run.options_strategies.filter(strategy => strategy.status === 'error').length;
        const strategies = run.options_strategies.map(strategy => `${strategy.name.replaceAll('_', ' ')}: ${strategy.status}${strategy.approved ? ' · approved' : ''}`).join(' · ');
        return `<div class="algo-research-detail-head"><div><span class="algo-eyebrow">OPTIONS RESEARCH RUN</span><h3>${escapeHtml(run.date || 'Undated run')}</h3></div><span class="algo-status-pill ${statusClass(run.status)}">${escapeHtml(run.status || 'unknown')}</span></div>
            <p class="algo-research-note">${escapeHtml(run.result || 'Options research result unavailable')}</p>
            <div class="algo-run-metrics"><div><span>Strategies</span><strong>${run.active_trials ?? 0}</strong></div><div><span>Completed</span><strong>${completed}</strong></div><div><span>Approved</span><strong>${approved}</strong></div><div><span>Errors</span><strong>${errors}</strong></div></div>
            <p class="algo-research-note">${escapeHtml(strategies || 'No strategy results')}</p>
            <small class="algo-detail-time">${run.created_at ? `Completed ${escapeHtml(dateTime(run.created_at))}` : 'Completion time unavailable'} · research only</small>`;
    }
    const strategyFitness = Array.isArray(run.strategy_fitness) ? run.strategy_fitness : [];
    return `<div class="algo-research-detail-head"><div><span class="algo-eyebrow">RESEARCH RUN</span><h3>${escapeHtml(run.date || 'Undated run')}</h3></div><span class="algo-status-pill ${statusClass(run.status)}">${escapeHtml(run.status || 'unknown')}</span></div>
        <p class="algo-research-note">${escapeHtml(run.result || 'Research result unavailable')}${run.portfolio_status && run.portfolio_status !== 'unavailable' ? ` · portfolio replay ${escapeHtml(run.portfolio_status)}` : ''}</p>
        ${run.factor_model ? `<div class="algo-run-metrics"><div><span>Factor model</span><strong>${escapeHtml(run.factor_model.family)}</strong></div><div><span>Observations</span><strong>${run.factor_model.observations ?? '—'}</strong></div><div><span>Rolling evaluations</span><strong>${run.factor_model.rolling_evaluations ?? '—'}</strong></div><div><span>Forward horizon</span><strong>${run.factor_model.forward_horizon_sessions ?? '—'} sessions</strong></div></div><p class="algo-research-note">Discovery ${escapeHtml(run.discovery_status)}${run.discovery_reason ? ` · ${escapeHtml(run.discovery_reason)}` : ''}</p>` : `<div class="algo-run-metrics"><div><span>Active trials</span><strong>${run.active_trials ?? '—'}</strong></div><div><span>Promoted</span><strong>${run.promoted ?? 0}</strong></div><div><span>Staged</span><strong>${run.staged ?? 0}</strong></div><div><span>Portfolio return</span><strong class="${classForReturn(run.portfolio_return_pct)}">${signedPercent(run.portfolio_return_pct)}</strong></div></div>`}
        ${strategyFitness.length ? `<section class="algo-strategy-fitness"><div class="algo-strategy-fitness-title"><strong>Strategy fitness</strong><span>Out-of-sample score</span></div><div class="algo-strategy-fitness-grid algo-strategy-fitness-header"><span>Strategy</span><span>Current</span><span>Δ vs last run</span></div>${strategyFitness.map(strategy => `<div class="algo-strategy-fitness-grid"><span>${escapeHtml(String(strategy.name || '').replaceAll('_', ' '))}</span><strong class="${classForReturn(strategy.fitness)}">${signedScore(strategy.fitness)}</strong><strong class="${classForReturn(strategy.delta_vs_previous)}"${strategy.previous_run_date ? ` title="Compared with ${escapeHtml(strategy.previous_run_date)}"` : ''}>${signedScore(strategy.delta_vs_previous)}</strong></div>`).join('')}</section>` : ''}
        <small class="algo-detail-time">${run.created_at ? `Completed ${escapeHtml(dateTime(run.created_at))}` : ''}${run.duration_seconds !== null && run.duration_seconds !== undefined ? ` · ${Math.round(run.duration_seconds / 60)} min` : ''}</small>`;
}

export function filterRuns(history, filter) {
    if (filter === 'promoted') return history.filter(run => run.promoted > 0);
    if (filter === 'no-promotion') return history.filter(run => !run.promoted);
    return history;
}

export function renderResearchPage({ mode, snapshot, filter, selectedDate, loading, error }) {
    const isOptions = mode === 'options';
    const modeLabel = isOptions ? 'Options' : 'Equity';
    if (!snapshot) {
        const html = `<main class="algo-page algo-research-page"><header class="algo-page-header"><div><div class="algo-breadcrumb">SYSTEMS <span>/</span> RESEARCH ARCHIVE</div><h1>Research</h1>${renderAccountSwitch({ active: mode, ariaLabel: 'Research account', attribute: 'data-research-mode', options: [['equity', 'Equity'], ['options', 'Options']] })}</div></header><section class="card algo-connection-card"><div class="algo-loading-mark">↻</div><strong>${loading ? `Loading ${modeLabel} research…` : `${modeLabel} research unavailable`}</strong><p>${escapeHtml(error || 'Loading saved strategy research records.')}</p><button type="button" class="btn btn-secondary" data-research-action="retry">Retry connection</button></section></main>`;
        return { html, selectedDate };
    }

    const research = snapshot.research || { history: [] };
    const demoMode = snapshot.demo_mode === true;
    const runs = filterRuns(snapshot.research?.history || [], filter);
    const activeDate = runs.some(run => run.date === selectedDate) ? selectedDate : runs[0]?.date || null;
    const selectedRun = runs.find(run => run.date === activeDate) || null;
        const title = isOptions ? 'Options research history' : 'Equity autoresearch history';
    const description = demoMode ? 'Example strategy research reports for preview only.' : isOptions ? 'Daily options strategy research reports.' : 'Daily saved research reports from the Algo Trader research archive.';

    const html = `<main class="algo-page algo-research-page">
        <header class="algo-page-header">
            <div><div class="algo-breadcrumb">SYSTEMS <span>/</span> RESEARCH ARCHIVE</div><h1>Research</h1>${renderAccountSwitch({ active: mode, ariaLabel: 'Research account', attribute: 'data-research-mode', options: [['equity', 'Equity'], ['options', 'Options']] })}</div>
            <div class="algo-header-status"><span class="algo-demo-tag"><i></i> ${demoMode ? 'DEMO RESEARCH' : `${modeLabel.toUpperCase()} RESEARCH`}</span></div>
        </header>
        <section class="card algo-panel algo-history-panel" id="algo-history">
            <div class="algo-section-heading algo-history-heading"><div><span class="algo-eyebrow">EXPERIMENT ARCHIVE</span><h2>${title}</h2><p>${description}</p></div><div class="algo-history-filters" role="group" aria-label="Filter research runs">${[['all', 'All runs'], ['promoted', isOptions ? 'Approved' : 'Promoted'], ['no-promotion', isOptions ? 'No approval' : 'No promotion']].map(([key, label]) => `<button type="button" data-research-filter="${key}" class="${filter === key ? 'active' : ''}" aria-pressed="${filter === key}">${label}</button>`).join('')}</div></div>
            <div class="algo-history-layout"><div class="algo-run-list" role="list">${runs.map(run => `<button type="button" role="listitem" class="algo-run-row ${run.date === activeDate ? 'selected' : ''}" data-run-date="${escapeHtml(run.date)}" aria-pressed="${run.date === activeDate}"><span class="algo-run-version">${escapeHtml(run.date || 'Undated')}<small>${run.created_at ? escapeHtml(timeOnly(run.created_at)) : 'saved report'}</small></span><span class="algo-run-name">${escapeHtml(run.result || 'Research report')}<small>${escapeHtml(run.options_strategies ? 'options research · research only' : run.portfolio_status || 'portfolio result unavailable')} · ${run.active_trials ?? '—'} ${isOptions ? 'strategies' : 'active trials'}</small></span><span class="algo-run-return ${classForReturn(run.portfolio_return_pct)}">${run.options_strategies ? `${run.promoted ?? 0} approved` : signedPercent(run.portfolio_return_pct)}<small>${isOptions ? 'paper config approval' : 'portfolio return'}</small></span><span class="algo-status-pill ${statusClass(run.status)}">${escapeHtml(run.status || 'unknown')}</span><span class="algo-run-chevron">›</span></button>`).join('') || '<div class="algo-empty">No reports match this filter.</div>'}</div>
                <aside class="algo-run-detail">${renderRunDetail(selectedRun)}</aside></div>
        </section>
    </main>`;
    return { html, selectedDate: activeDate };
}
