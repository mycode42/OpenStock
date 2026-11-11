import { apiRequest } from '../api.js';
import { escapeHtml } from '../escape.js';
import { renderSystemGauge, startSystemGauge } from '../system-gauge.js?system-gauge=1';

// Interactive research: start a run, follow its log, read results, and promote a profile with rollback.
// The lab keeps its DOM node across the archive refresh, and re-renders only when its own state changes.
const POLL_MS = 1000;
const LOG_TAIL_LINES = 200;

const state = {
    account: 'options',
    overview: null,
    strategy: null,
    form: { underlyings: '', start: '', end: '', profile: '' },
    picks: { equity: [], options: [] },
    equity: { underlyings: '', start: '', end: '' },
    plan: {},
    planPreview: null,
    planInfo: null,
    job: null,
    results: null,
    // Which run the results panel shows. Chosen by the user; follows the newest run until then.
    resultsJobId: null,
    resultsFor: null,
    // The entry and exit rules stay open or closed across strategy changes.
    rulesOpen: false,
    logText: '',
    logOffset: 0,
    logOpen: false,
    timer: null,
    message: '',
    error: '',
    saveName: '',
    preview: null,
};
const boundHosts = new WeakSet();

function host() {
    return document.getElementById('research-lab-host');
}

function tail(text) {
    return String(text || '').split('\n').slice(-LOG_TAIL_LINES).join('\n');
}

// One line for the collapsed log: the last error if the run has printed one, otherwise the last line.
function logSummary(text) {
    const lines = tail(text).split('\n').map(line => line.trim()).filter(Boolean);
    if (!lines.length) return { kind: 'Status', line: 'Waiting for output…', error: false };
    const lastError = lines.slice().reverse().find(line => /\b(error|traceback|exception|failed|failure)\b/i.test(line));
    return lastError
        ? { kind: 'Last error', line: lastError, error: true }
        : { kind: 'Last line', line: lines[lines.length - 1], error: false };
}

function percent(job) {
    if (Number.isFinite(job?.progress?.percent)) return job.progress.percent;
    const total = job?.progress?.total || 0;
    const done = job?.progress?.done || 0;
    return total ? Math.round((done / total) * 100) : 0;
}

function isRunning(job) {
    return job?.state === 'running' || job?.state === 'cancelling';
}

function strategies() {
    return state.overview?.strategies || [];
}

function current() {
    return strategies().find(item => item.id === state.strategy) || null;
}

function renderStrategyTabs() {
    const tabs = strategies().map(item => `<button type="button" class="lab-tab ${item.id === state.strategy ? 'active' : ''}" data-lab-strategy="${escapeHtml(item.id)}">${escapeHtml(item.id.replaceAll('_', ' '))}</button>`).join('');
    return `<div class="lab-tabs" role="tablist">${tabs}</div>`;
}

const STAGE_STATUS_LABELS = {
    pending: 'waiting', running: 'running', completed: 'done', reused: 'reused', skipped: 'skipped',
    not_run: 'not run', error: 'error', failed: 'failed',
};

// Equity runs: one row per pipeline stage, a percentage, and a clear finished state.
function renderEquityLive(job) {
    const stages = job.stages;
    const running = isRunning(job);
    const current = stages.rows.find(row => row.status === 'running');
    const done = job.state === 'finished';
    let headline;
    const next = stages.rows.find(row => row.status === 'pending');
    if (running) headline = `Running${current ? ` · ${escapeHtml(current.label)}${current.strategies ? ` (${current.strategies.done} of ${current.strategies.total} strategies)` : ''}` : next ? ` · next: ${escapeHtml(next.label)}` : ' · starting'}`;
    else if (done) headline = `Finished · report ${escapeHtml(state.resultsFor === job.id ? state.results?.report?.status ?? 'unknown' : 'unknown')}`;
    else headline = `${escapeHtml(job.state)}: open the log below for the error`;
    const rows = stages.rows.map(row => {
        const label = STAGE_STATUS_LABELS[row.status] || row.status;
        const count = row.strategies ? ` · ${row.strategies.done} of ${row.strategies.total} strategies` : '';
        return `<li class="lab-stage is-${escapeHtml(row.status)}"><span class="lab-stage-name">${escapeHtml(row.label)}</span><span class="lab-stage-status">${escapeHtml(label)}${count}</span></li>`;
    }).join('');
    return `<div class="lab-live ${done ? 'is-finished' : ''}">
        <div class="lab-live-head"><strong>${escapeHtml((job.strategies || []).map(item => item.replaceAll('_', ' ')).join(', '))}</strong> <span class="lab-muted">${escapeHtml(job.id)}</span></div>
        <p class="lab-live-state"><strong>${headline}</strong></p>
        <div class="lab-progress" role="progressbar" aria-label="Run progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${stages.percent}"><div style="width:${stages.percent}%"></div></div>
        <p class="lab-muted">${stages.percent}%${stages.current_stage ? ` · stage ${escapeHtml(stages.current_stage.replaceAll('_', ' '))}` : ''}</p>
        ${renderReplayDetail(job)}
        <ol class="lab-stages">${rows}</ol>
        ${running ? '<button type="button" class="btn btn-secondary" data-lab-action="cancel">Cancel run</button>' : ''}
        ${renderLogToggle()}
    </div>`;
}

// The other equity runs going at the same time, each with its own bar.
function renderOtherRuns() {
    const others = (state.overview?.active_jobs || []).filter(item => item.id !== state.job?.id && item.account === state.account);
    if (!others.length) return '';
    const rows = others.map(item => `<li><span>${escapeHtml(item.id)}</span><div class="lab-progress"><div style="width:${item.percent ?? 0}%"></div></div><span class="lab-muted">${item.percent ?? 0}%</span></li>`).join('');
    return `<div class="lab-other-runs"><p class="lab-muted">Also running</p><ul>${rows}</ul></div>`;
}

function renderReplayDetail(job) {
    const replays = Object.values(job.replay || {});
    if (!replays.length || !isRunning(job)) return '';
    return replays.map(item => `<p class="lab-muted">${escapeHtml(item.label)}: ${item.sessions_done} of ${item.sessions_total} sessions${item.through ? `, through ${escapeHtml(item.through)}` : ''} · about ${Math.max(0, Math.round(item.remaining_seconds))}s left</p>`).join('');
}

function renderLive() {
    const job = state.job;
    if (!job || job.account !== state.account) return '<p class="lab-muted">No run in this session yet.</p>';
    if (job.account === 'equity' && job.stages) return renderEquityLive(job) + renderOtherRuns();
    const progress = job.progress || {};
    const extras = [
        progress.current ? `now ${escapeHtml(progress.current)}` : '',
        progress.failures ? `${progress.failures} failed` : '',
        progress.approved?.length ? `approved ${escapeHtml(progress.approved.join(', '))}` : '',
    ].filter(Boolean).join(' · ');
    return `<div class="lab-live">
        <div class="lab-live-head"><strong>${escapeHtml(job.strategy)} · ${escapeHtml(job.state)}</strong> <span class="lab-muted">${escapeHtml(job.id)}</span></div>
        <div class="lab-progress"><div style="width:${percent(job)}%"></div></div>
        <p class="lab-muted">${progress.done || 0} of ${progress.total || 0} underlyings${extras ? ` · ${extras}` : ''}</p>
        ${isRunning(job) ? '<button type="button" class="btn btn-secondary" data-lab-action="cancel">Cancel run</button>' : ''}
        ${renderLogToggle()}
    </div>`;
}

function renderLogToggle() {
    const summary = logSummary(state.logText);
    const toggle = `<button type="button" class="lab-log-toggle" data-lab-action="toggle-log" aria-expanded="${state.logOpen}" aria-controls="lab-log-full">
        <span class="lab-log-kind ${summary.error ? 'lab-bad' : ''}">${escapeHtml(summary.kind)}</span>
        <span class="lab-log-line">${escapeHtml(summary.line)}</span>
        <span class="lab-log-caret">${state.logOpen ? 'Hide' : 'Show log'}</span>
    </button>`;
    const full = state.logOpen
        ? `<pre class="lab-log" id="lab-log-full" aria-label="Live research log">${escapeHtml(tail(state.logText) || 'Waiting for output…')}</pre>`
        : '';
    return toggle + full;
}

// Strategies to run, each with its own profile. The same table for equity and options.
function renderRunTable() {
    const picked = new Set(state.picks[state.account]);
    const rows = strategies().map(item => {
        const profiles = item.profiles || [];
        const chosen = state.plan[item.id] || 'standard';
        const select = profiles.length
            ? `<select class="lab-run-profile" data-lab-plan="${escapeHtml(item.id)}" aria-label="Profile for ${escapeHtml(item.id)}">${profiles.map(profile => `<option value="${escapeHtml(profile.name)}" ${profile.name === chosen ? 'selected' : ''}>${escapeHtml(profile.name)}</option>`).join('')}</select>`
            : '<span class="lab-muted">no profile settings</span>';
        return `<div class="lab-run-row ${picked.has(item.id) ? 'is-picked' : ''}">
            <label class="research-switch"><input type="checkbox" role="switch" data-lab-pick="${escapeHtml(item.id)}" ${picked.has(item.id) ? 'checked' : ''} aria-label="Run ${escapeHtml(item.id)}"></label>
            <span class="lab-run-name">${escapeHtml(item.id.replaceAll('_', ' '))}</span>
            ${select}
        </div>`;
    }).join('');
    const count = picked.size;
    return `<div class="lab-run-table" role="group" aria-label="Strategies to run">
        <div class="lab-run-head"><span></span><span>Strategy</span><span>Profile</span></div>
        ${rows}
        <div class="lab-run-tools">
            <span class="lab-muted">${count ? `${count} selected` : 'Nothing selected'}</span>
            <button type="button" class="btn btn-sm btn-secondary" data-lab-action="select-all-picks">Select all</button>
            <button type="button" class="btn btn-sm btn-secondary" data-lab-action="clear-picks" ${count ? '' : 'disabled'}>Clear</button>
        </div>
    </div>`;
}

function renderEquityStart() {
    const busy = (state.overview.active_jobs || []).length >= (state.overview.limits?.concurrentJobs || 1);
    const picked = state.picks.equity.length;
    return `<form class="lab-form" data-lab-form="start-equity">
        ${renderRunTable()}
        <label>Universe <span class="lab-muted">(optional: one or more tickers with stored bars. Empty = the live universe)</span>
            <input type="text" data-lab-equity-field="underlyings" value="${escapeHtml(state.equity.underlyings)}" placeholder="empty = live universe"></label>
        <div class="lab-dates">
            <label>Start <input type="date" data-lab-equity-field="start" min="2020-01-01" value="${escapeHtml(state.equity.start)}"></label>
            <label>End <input type="date" data-lab-equity-field="end" value="${escapeHtml(state.equity.end)}"></label>
        </div>
        <button type="submit" class="btn btn-primary" ${busy || !picked || state.overview?.read_only ? 'disabled' : ''}>Start research</button>
        ${busy ? `<p class="lab-muted">Run ${escapeHtml(state.overview.active_job)} is already running.</p>` : !picked ? '<p class="lab-muted">Switch on at least one strategy to start.</p>' : ''}
    </form>`;
}

// Profile plan: the same panel for equity and options. Preview shows the changes, Apply writes them, Undo restores the last apply.
function renderPlanPreview(preview) {
    if (!preview.strategies.length) return '<p class="lab-muted">Nothing to change: the chosen profiles are the live values.</p>';
    const rows = preview.strategies.flatMap(item => item.diff.filter(row => row.changed).map(row => `<tr class="lab-changed"><td>${escapeHtml(item.strategy.replaceAll('_', ' '))}</td><td>${escapeHtml(item.profile)}</td><td>${escapeHtml(row.key)}</td><td>${escapeHtml(JSON.stringify(row.current))}</td><td>${escapeHtml(JSON.stringify(row.proposed))}</td></tr>`)).join('');
    const summary = preview.changes
        ? `<p><strong>${preview.changes} setting(s) change</strong> in ${preview.strategies.length} strategy(ies). Nothing is written until you press Apply.</p>`
        : '<p class="lab-muted">Nothing to change: the chosen profiles are the live values.</p>';
    return `${summary}${preview.changes ? `<table class="lab-table"><thead><tr><th>Strategy</th><th>Profile</th><th>Setting</th><th>Live now</th><th>After apply</th></tr></thead><tbody>${rows}</tbody></table>` : ''}`;
}

function formatWhen(value) {
    return value ? new Date(value).toLocaleString() : '';
}

function renderPlanPanel() {
    const where = state.account === 'options' ? 'options' : 'equity';
    const chosen = Object.entries(state.plan).filter(([, name]) => name && name !== 'standard');
    const chosenList = chosen.length
        ? chosen.map(([id, name]) => `<li>${escapeHtml(id.replaceAll('_', ' '))}: <strong>${escapeHtml(name)}</strong></li>`).join('')
        : '<li class="lab-muted">Every strategy uses the standard profile, the live values.</li>';
    const live = state.planInfo?.last_applied;
    const liveLine = live
        ? `Applied ${escapeHtml(formatWhen(live.at))}: ${live.profiles.map(escapeHtml).join(', ')}`
        : 'Nothing applied from the lab yet. The live settings are the standard values.';
    const history = (state.planInfo?.history || []).map(item => `<li>${escapeHtml(formatWhen(item.at))} · ${item.changes ?? 0} change(s) · ${item.profiles.map(escapeHtml).join(', ')}${item.rolled_back_at ? ' · undone' : ''}</li>`).join('');
    const preview = state.planPreview;
    return `<section class="lab-plan" aria-label="Profile plan">
        <h3>Profile plan (${where})</h3>
        <ul class="lab-plan-list">${chosenList}</ul>
        <p class="lab-plan-live"><strong>Live now:</strong> ${liveLine}</p>
        <div class="lab-plan-actions">
            <button type="button" class="btn btn-secondary" data-lab-action="plan-preview">1 · Preview changes</button>
            <button type="button" class="btn btn-primary" data-lab-action="plan-apply" ${!state.overview?.read_only && preview && preview.changes ? '' : 'disabled'}>2 · Apply changes</button>
            <button type="button" class="btn btn-secondary" data-lab-action="plan-undo" ${!state.overview?.read_only && live ? '' : 'disabled'}>Undo last apply</button>
        </div>
        ${preview ? renderPlanPreview(preview) : ''}
        ${history ? `<details class="lab-plan-history"><summary>Recent applies</summary><ul class="lab-history">${history}</ul></details>` : ''}
    </section>`;
}

// A replay of the chosen parameters over the chosen history: P/L, trades and drawdown, per strategy and per regime.
function renderReplay(replay) {
    const s = replay.summary || {};
    const pct = value => typeof value === 'number' ? `${value.toFixed(2)}%` : '—';
    const usd = value => typeof value === 'number' ? `$${Math.round(value).toLocaleString('en-US')}` : '—';
    const count = value => typeof value === 'number' ? String(value) : '—';
    const table = (groups, label) => {
        const rows = Object.entries(groups).map(([name, g]) => `<tr><td>${escapeHtml(name.replaceAll('_', ' '))}</td>
            <td>${count(g.closed_trades)}</td><td>${usd(g.pnl_usd)}</td><td>${pct(g.pnl_pct_of_initial_capital)}</td>
            <td>${pct(g.win_rate_pct)}</td><td>${usd(g.average_trade_usd)}</td><td>${typeof g.profit_factor === 'number' ? escapeHtml(g.profit_factor.toFixed(2)) : '—'}</td></tr>`).join('');
        return rows ? `<table class="lab-table"><thead><tr><th>${label}</th><th>Trades</th><th>P/L</th><th>P/L %</th><th>Win rate</th><th>Average trade</th><th>Profit factor</th></tr></thead><tbody>${rows}</tbody></table>` : '<p class="lab-muted">No closed trades in this window.</p>';
    };
    const window = replay.window ? `${escapeHtml(replay.window.start)} to ${escapeHtml(replay.window.end)}` : '—';
    const params = Object.entries(replay.parameters || {}).map(([name, values]) => `<li><strong>${escapeHtml(name.replaceAll('_', ' '))}</strong>: <code>${escapeHtml(JSON.stringify(Object.fromEntries(Object.entries(values).filter(([key]) => typeof values[key] !== 'object'))))}</code></li>`).join('');
    const live = replay.live;
    const ls = live?.summary || {};
    const comparison = live ? `<h4>Chosen parameters against live settings</h4>
        <table class="lab-table"><thead><tr><th></th><th>Chosen</th><th>Live</th><th>Difference</th></tr></thead><tbody>
        <tr><td>Net return</td><td>${pct(s.net_return_pct)}</td><td>${pct(ls.net_return_pct)}</td><td>${typeof s.net_return_pct === 'number' && typeof ls.net_return_pct === 'number' ? pct(s.net_return_pct - ls.net_return_pct) : '—'}</td></tr>
        <tr><td>Max drawdown</td><td>${pct(s.max_drawdown_pct)}</td><td>${pct(ls.max_drawdown_pct)}</td><td>${typeof s.max_drawdown_pct === 'number' && typeof ls.max_drawdown_pct === 'number' ? pct(s.max_drawdown_pct - ls.max_drawdown_pct) : '—'}</td></tr>
        <tr><td>Closed trades</td><td>${count(s.closed_trade_count)}</td><td>${count(ls.closed_trade_count)}</td><td>${typeof s.closed_trade_count === 'number' && typeof ls.closed_trade_count === 'number' ? s.closed_trade_count - ls.closed_trade_count : '—'}</td></tr>
        <tr><td>Buy and hold</td><td>${pct(s.benchmark_net_return_pct)}</td><td>${pct(ls.benchmark_net_return_pct)}</td><td></td></tr>
        </tbody></table>
        <h4>P/L by strategy, live settings</h4>${table(live.strategy_breakdown?.by_strategy || {}, 'Strategy')}` : '';
    return `<p><strong>Replay ${window}</strong>${replay.universe_size ? ` · ${escapeHtml(replay.universe_size)} tickers` : ''}</p>
        <p>Net return <strong>${pct(s.net_return_pct)}</strong> · buy and hold ${pct(s.benchmark_net_return_pct)} · max drawdown <strong>${pct(s.max_drawdown_pct)}</strong> · <strong>${count(s.closed_trade_count)}</strong> closed trades (${count(s.entry_count)} entries)</p>
        ${comparison}
        <h4>By strategy${live ? ', chosen parameters' : ''}</h4>${table(replay.by_strategy || {}, 'Strategy')}
        <h4>By entry regime</h4>${table(replay.by_regime || {}, 'Regime')}
        <details class="lab-rules"><summary>Parameters replayed</summary><ul class="lab-rule-list">${params}</ul></details>`;
}

function renderEquityResults(data) {
    if (data.replay) {
        const head = data.report ? `<p class="lab-muted">Report status: ${escapeHtml(data.report.status ?? 'unknown')}${data.report.created_at ? ` · ${escapeHtml(data.report.created_at)}` : ''}</p>` : '';
        return `${head}${renderReplay(data.replay)}`;
    }
    const report = data.report
        ? `<p class="lab-muted">Report status: ${escapeHtml(data.report.status ?? 'unknown')}${data.report.created_at ? ` · ${escapeHtml(data.report.created_at)}` : ''}</p>${data.report.discovery_only_reason ? `<p class="lab-muted">Discovery only, not verified out of sample: ${escapeHtml(data.report.discovery_only_reason)}. Nothing from this run can be applied or promoted.</p>` : ''}`
        : '<p class="lab-muted">The report appears here when the run finishes.</p>';
    const pct = value => typeof value === 'number' ? `${value.toFixed(2)}%` : '—';
    const count = value => typeof value === 'number' ? String(value) : '—';
    const money = value => typeof value === 'number' ? `$${Math.round(value).toLocaleString('en-US')}` : '—';
    const rows = (data.strategies || []).map(item => {
        const metrics = item.metrics;
        const start = metrics?.start;
        const best = metrics?.best;
        const selection = metrics?.selection_status === 'no_adaptive_improvement' ? 'no candidate beat the start' : (metrics?.selection_status ?? '—');
        return `<tr><td>${escapeHtml(item.strategy.replaceAll('_', ' '))}</td><td>${escapeHtml(item.status ?? '—')}</td>
            <td>${pct(start?.net_return_pct)} · ${count(start?.closed_trades)} trades</td>
            <td>${pct(best?.net_return_pct)} · ${count(best?.closed_trades)} trades${best?.eligible === false ? ' <span class="lab-muted">(not eligible)</span>' : ''}</td>
            <td>${pct(best?.max_drawdown_pct)}</td><td>${money(best?.final_equity)}</td>
            <td>${escapeHtml(selection)}</td></tr>`;
    }).join('');
    const window = data.strategies?.find(item => item.metrics?.window)?.metrics?.window;
    const note = window ? `<p class="lab-muted">In-sample, ${escapeHtml(window.start)} to ${escapeHtml(window.end)} (${escapeHtml(window.completed_sessions)} sessions), net of costs. Start is the profile the run began with. Best is the strongest candidate the search found.</p>` : '';
    return `${report}${rows ? `<table class="lab-table lab-results"><thead><tr><th>Strategy</th><th>Status</th><th>Start return</th><th>Best return</th><th>Best max drawdown</th><th>Best final equity</th><th>Selection</th></tr></thead><tbody>${rows}</tbody></table>${note}` : ''}`;
}

// The entry and exit rules of the selected strategy, with the live parameter values filled in.
function renderRules(strategy) {
    const rules = strategy?.rules;
    if (!rules || (!rules.entry?.length && !rules.exit?.length)) return '';
    const list = items => `<ul class="lab-rule-list">${items.map(item => `<li>${escapeHtml(item)}</li>`).join('')}</ul>`;
    return `<details class="lab-rules" data-lab-rules ${state.rulesOpen ? 'open' : ''}><summary>Entry and exit rules: ${escapeHtml(strategy.id.replaceAll('_', ' '))}</summary>
        <div class="lab-rules-grid"><div><strong>Entry</strong>${list(rules.entry)}</div><div><strong>Exit</strong>${list(rules.exit)}</div></div>
    </details>`;
}

const ENUM_CHOICES = Object.freeze({
    slope_mode: ['fixed_pct', 'volatility_scaled', 'off'],
    regime_filter: ['none', 'er', 'adx', 'combined'],
});

// The built-in profiles for the selected equity strategy, side by side with its live values.
function renderProfilePanel(strategy) {
    const profiles = strategy?.profiles || [];
    if (!profiles.length) return '';
    const keys = [...new Set(profiles.flatMap(profile => Object.keys(profile.parameters)))].filter(key => profiles.every(profile => ['number', 'string'].includes(typeof profile.parameters[key]) || profile.parameters[key] === undefined));
    const live = strategy.parameters || {};
    const head = `<tr><th>Parameter</th><th>Live now</th>${profiles.map(profile => `<th>${escapeHtml(profile.name)}</th>`).join('')}</tr>`;
    const rows = keys.map(key => `<tr><th>${escapeHtml(key)}</th><td>${escapeHtml(JSON.stringify(live[key] ?? '—'))}</td>${profiles.map(profile => `<td>${escapeHtml(JSON.stringify(profile.parameters[key] ?? '—'))}</td>`).join('')}</tr>`).join('');
    const custom = keys.map(key => {
        const options = ENUM_CHOICES[key];
        if (options) {
            return `<label>${escapeHtml(key)} <select data-lab-custom-param="${escapeHtml(key)}">${options.map(option => `<option value="${escapeHtml(option)}" ${option === live[key] ? 'selected' : ''}>${escapeHtml(option)}</option>`).join('')}</select></label>`;
        }
        return `<label>${escapeHtml(key)} <input type="number" step="any" min="0" data-lab-custom-param="${escapeHtml(key)}" value="${escapeHtml(live[key] ?? '')}"></label>`;
    }).join('');
    return `<h3>Profiles</h3>
        <p class="lab-muted">Standard is the live values. Conservative and high-risk move the strategy settings; the frozen gates and portfolio caps stay as they are.</p>
        <table class="lab-table lab-params-wide"><thead>${head}</thead><tbody>${rows}</tbody></table>
        <details class="lab-custom" open><summary>Custom profile</summary>
            <label>Name <input type="text" data-lab-custom-name maxlength="40" placeholder="my-profile"></label>
            <div class="lab-custom-grid">${custom}</div>
            <button type="button" class="btn btn-secondary" data-lab-action="save-custom" ${state.overview?.read_only ? 'disabled' : ''}>Save custom profile</button>
        </details>`;
}

// Every run the results can come from: the one in the live panel, the running ones, then the recent finished ones.
function resultRuns() {
    const seen = new Set();
    const runs = [];
    const add = (id, runState) => {
        if (!id || seen.has(id)) return;
        seen.add(id);
        runs.push({ id, state: runState });
    };
    if (state.job?.account === state.account) add(state.job.id, state.job.state);
    for (const job of state.overview?.active_jobs || []) if (job.account === state.account) add(job.id, 'running');
    for (const job of state.overview?.recent_jobs || []) add(job.id, job.state);
    return runs;
}

function renderResults() {
    const runs = resultRuns();
    const selected = state.resultsJobId || state.job?.id || null;
    const picker = runs.length > 1
        ? `<label class="lab-results-pick">Run <select data-lab-results-run>${runs.map(run => `<option value="${escapeHtml(run.id)}" ${run.id === selected ? 'selected' : ''}>${escapeHtml(run.id)} · ${escapeHtml(run.state)}</option>`).join('')}</select></label>`
        : '';
    return `${picker}<div id="lab-results-body">${renderResultsBody()}</div>`;
}

function renderResultsOnly() {
    const body = document.getElementById('lab-results-body');
    if (body) body.innerHTML = renderResultsBody();
}

function renderResultsBody() {
    const data = state.results;
    if (data?.account === 'equity') return renderEquityResults(data);
    if (!data?.results?.length) return '<p class="lab-muted">Results appear here as each underlying finishes.</p>';
    const tunable = current()?.tunable || [];
    const rows = data.results.map(item => {
        if (item.status === 'error') {
            return `<tr><td>${escapeHtml(item.underlying)}</td><td colspan="4" class="lab-bad">${escapeHtml(item.error || 'error')}</td></tr>`;
        }
        const sharpe = item.oos_metrics?.annualized_sharpe;
        const params = Object.fromEntries(tunable.filter(key => key in (item.final_parameters || {})).map(key => [key, item.final_parameters[key]]));
        const ex = item.exploratory;
        const money = value => `$${Math.round(value).toLocaleString('en-US')}`;
        const replayText = (label, value) => value ? `<tr class="lab-exploratory"><td></td><td colspan="4">${label}: ${typeof value.trades === 'number' ? `${value.trades} trades` : '—'}, Sharpe ${typeof value.annualized_sharpe === 'number' ? escapeHtml(value.annualized_sharpe.toFixed(2)) : '—'}, average trade ${typeof value.average_trade_pnl === 'number' ? escapeHtml(money(value.average_trade_pnl)) : '—'}, equity ${typeof value.start_equity === 'number' && typeof value.end_equity === 'number' ? `${escapeHtml(money(value.start_equity))} to ${escapeHtml(money(value.end_equity))}` : '—'}.</td></tr>` : '';
        const exploratoryRow = replayText('Replay of the chosen parameters (not validated out of sample)', ex) + replayText('Replay of the live settings, same dates', item.live_exploratory);
        return `<tr><td>${escapeHtml(item.underlying)}</td><td>${escapeHtml(item.status)}${item.approved ? ' · approved' : ''}</td>
            <td>${typeof sharpe === 'number' ? escapeHtml(sharpe.toFixed(2)) : '—'}</td><td>${item.observed_oos_trades ?? '—'}</td>
            <td><code>${escapeHtml(JSON.stringify(params))}</code></td></tr>${exploratoryRow}`;
    }).join('');
    return `<table class="lab-table lab-results"><thead><tr><th>Underlying</th><th>Status</th><th>OOS Sharpe</th><th>OOS trades</th><th>Final tunable parameters</th></tr></thead><tbody>${rows}</tbody></table>
        <div class="lab-save"><input type="text" data-lab-input="saveName" placeholder="profile name" value="${escapeHtml(state.saveName)}" maxlength="40">
        <button type="button" class="btn btn-secondary" data-lab-action="save-profile">Save first finished parameters as profile</button></div>`;
}

function renderStartForm() {
    const busy = (state.overview.active_jobs || []).length >= (state.overview.limits?.concurrentJobs || 1);
    const picked = state.picks.options.length;
    const queued = (state.overview.queued || []);
    return `<form class="lab-form" data-lab-form="start">
        ${renderRunTable()}
        <label>Underlyings (max ${state.overview.limits.maxUnderlyings}, comma separated)
            <input type="text" name="underlyings" data-lab-field value="${escapeHtml(state.form.underlyings)}" placeholder="SPY, QQQ, GLD" required></label>
        <div class="lab-dates">
            <label>Start <input type="date" name="start" data-lab-field value="${escapeHtml(state.form.start)}" min="${escapeHtml(state.overview.limits.earliestStart)}"></label>
            <label>End <input type="date" name="end" data-lab-field value="${escapeHtml(state.form.end)}"></label>
        </div>
        <button type="submit" class="btn btn-primary" ${busy || !picked || state.overview?.read_only ? 'disabled' : ''}>Start research</button>
        ${busy ? `<p class="lab-muted">Run ${escapeHtml(state.overview.active_job)} is already running.</p>` : !picked ? '<p class="lab-muted">Switch on at least one strategy to start.</p>' : ''}
        ${queued.length ? `<p class="lab-muted">Queued after it: ${queued.map(escapeHtml).join(', ')}</p>` : ''}
    </form>`;
}

function renderLab() {
    if (!state.overview) {
        const message = state.error ? `<p class="lab-bad">Research lab could not load: ${escapeHtml(state.error)}</p>` : '<p class="lab-muted">Loading research lab…</p>';
        return `<section class="card algo-panel lab-panel">${message}</section>`;
    }
    const notice = [
        state.message ? `<p class="lab-ok">${escapeHtml(state.message)}</p>` : '',
        state.error ? `<p class="lab-bad">${escapeHtml(state.error)}</p>` : '',
    ].join('');
    const isOptions = state.account === 'options';
    const strategy = current();
    const heading = isOptions
        ? '<p>One job at a time, on the shared universe. Lab runs never change the live champion; promotion is a separate, confirmed step.</p>'
        : '<p>Pick the strategies, profiles and dates for a run. Every run uses the latest data and writes to its own folder, never the nightly report.</p>';
    const body = isOptions
        ? `<div class="lab-grid">
            <div><h3>Progress and log</h3><div id="lab-live">${renderLive()}</div><h3>Results</h3><div id="lab-results">${renderResults()}</div></div>
            <div><h3>Start a run</h3>${renderStartForm()}</div>
          </div>
          <div class="lab-fullwidth">${renderStrategyTabs()}</div>
          <div class="lab-fullwidth">${renderRules(strategy)}</div>
          <div class="lab-fullwidth">${renderProfilePanel(strategy)}</div>
          <div class="lab-fullwidth">${renderPlanPanel()}</div>
          `
        : `<div class="lab-grid"><div><h3>Progress and log</h3><div id="lab-live">${renderLive()}</div><h3>Results</h3><div id="lab-results">${renderResults()}</div></div><div><h3>Start a run</h3>${renderEquityStart()}</div></div>
          <div class="lab-fullwidth">${renderStrategyTabs()}</div>
          <div class="lab-fullwidth">${renderRules(strategy)}</div>
          <div class="lab-fullwidth">${renderProfilePanel(strategy)}</div>
          <div class="lab-fullwidth">${renderPlanPanel()}</div>`;
    return `<section class="card algo-panel lab-panel" id="research-lab">
        <div class="lab-head"><div class="algo-section-heading"><div><span class="algo-eyebrow">RESEARCH LAB</span><h2>Interactive ${isOptions ? 'options' : 'equity'} research</h2>${heading}</div></div>
            <div id="system-gauge" class="sys-gauge" aria-label="Host load"></div></div>
        ${notice}
        ${state.overview.read_only ? '<p class="lab-muted">Demo: sample profiles and results. Runs, plan changes and saved profiles are disabled.</p>' : ''}
        ${body}
    </section>`;
}

function render() {
    const element = host();
    if (!element) return;
    element.innerHTML = renderLab();
    startSystemGauge();
    renderSystemGauge();
}

function stopPolling() {
    if (state.timer) clearInterval(state.timer);
    state.timer = null;
}

function startPolling() {
    stopPolling();
    state.timer = setInterval(pollJob, POLL_MS);
}

async function refreshOverview() {
    const requested = state.account;
    const overview = await apiRequest(`/algo-trader/research/lab?account=${encodeURIComponent(requested)}`, {}, message => { state.error = message; });
    if (requested !== state.account) return;
    const plan = await apiRequest(`/algo-trader/research/plan?account=${encodeURIComponent(requested)}`);
    if (requested === state.account && plan) { state.plan = plan.profiles || {}; state.planInfo = plan; }
    if (overview) {
        state.overview = overview;
        if (!strategies().some(item => item.id === state.strategy)) state.strategy = strategies()[0]?.id || null;
    }
    if (overview?.active_job && !state.job) {
        const job = await apiRequest(`/algo-trader/research/jobs/${encodeURIComponent(overview.active_job)}`);
        if (job) {
            state.job = job;
            state.logText = '';
            state.logOffset = 0;
        }
    }
    if (isRunning(state.job)) startPolling();
    render();
}

// Show the results of the run picked in the list. Results of a run still going are read again while it runs.
async function selectResults(id) {
    state.resultsJobId = id;
    state.results = null;
    state.resultsFor = null;
    renderResultsOnly();
    const results = await apiRequest(`/algo-trader/research/jobs/${encodeURIComponent(id)}/results`, {}, message => { state.error = message; });
    if (state.resultsJobId !== id) return;
    state.results = results;
    state.resultsFor = id;
    renderResultsOnly();
}

async function pollJob() {
    const id = state.job?.id;
    if (!id) return stopPolling();
    const job = await apiRequest(`/algo-trader/research/jobs/${encodeURIComponent(id)}`);
    if (!job) return undefined;
    state.job = job;
    const chunk = await apiRequest(`/algo-trader/research/jobs/${encodeURIComponent(id)}/log?offset=${state.logOffset}`);
    if (chunk) {
        state.logText += chunk.text;
        state.logOffset = chunk.next;
    }
    if (!isRunning(job)) {
        stopPolling();
        state.results = await apiRequest(`/algo-trader/research/jobs/${encodeURIComponent(id)}/results`);
        state.resultsJobId = id;
        state.resultsFor = id;
        state.message = `Run ${job.state}.`;
        return refreshOverview();
    }
    state.pollTick = (state.pollTick || 0) + 1;
    if (state.pollTick % 3 === 0) {
        const overview = await apiRequest(`/algo-trader/research/lab?account=${encodeURIComponent(state.account)}`);
        if (overview && overview.account === state.account) state.overview = overview;
        const selected = state.resultsJobId;
        if (selected && selected !== id && (state.overview?.active_jobs || []).some(item => item.id === selected)) {
            const results = await apiRequest(`/algo-trader/research/jobs/${encodeURIComponent(selected)}/results`);
            if (state.resultsJobId === selected && results) {
                state.results = results;
                state.resultsFor = selected;
                renderResultsOnly();
            }
        }
    }
    // Only the live panel updates while a run is going, so the form and any typing are untouched.
    renderLiveOnly();
    return undefined;
}

function renderLiveOnly() {
    const live = document.getElementById('lab-live');
    if (live) live.innerHTML = renderLive();
}

async function postJson(url, body) {
    return apiRequest(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    }, message => { state.error = message; });
}

async function startEquityRun() {
    state.error = '';
    const tickers = state.equity.underlyings.split(/[\s,]+/).filter(Boolean);
    const job = await postJson('/algo-trader/research/jobs', {
        account: 'equity', strategies: state.picks.equity,
        underlyings: tickers, start: state.equity.start || null, end: state.equity.end || null,
        profiles: state.plan,
    });
    if (job) {
        state.job = job;
        state.results = null;
        state.resultsJobId = job.id;
        state.resultsFor = null;
        state.logText = '';
        state.logOffset = 0;
        state.message = `Started ${job.id}.`;
        startPolling();
    }
    render();
}

async function startRun() {
    if (state.account === 'equity') return startEquityRun();
    state.error = '';
    const job = await postJson('/algo-trader/research/jobs', {
        account: 'options',
        strategies: state.picks.options,
        underlyings: state.form.underlyings.split(/[\s,]+/).filter(Boolean),
        start: state.form.start || null,
        end: state.form.end || null,
        profiles: state.plan,
    });
    if (job) {
        state.job = job;
        state.results = null;
        state.resultsJobId = job.id;
        state.resultsFor = null;
        state.logText = '';
        state.logOffset = 0;
        state.message = `Started ${job.id}.`;
        startPolling();
    }
    render();
}

async function handleAction(action) {
    state.error = '';
    if (action === 'select-all-picks') {
        state.picks[state.account] = strategies().map(item => item.id);
    } else if (action === 'clear-picks') {
        state.picks[state.account] = [];
    } else if (action === 'plan-preview') {
        state.planPreview = await apiRequest(`/algo-trader/research/plan/preview?account=${encodeURIComponent(state.account)}`, {}, message => { state.error = message; });
    } else if (action === 'plan-apply') {
        const changes = state.planPreview?.changes || 0;
        if (!changes) return undefined;
        if (!window.confirm(`Apply ${changes} change(s) to the live ${state.account} settings? Undo restores the settings from before.`)) return undefined;
        const result = await postJson('/algo-trader/research/plan/apply', { account: state.account });
        if (result) state.message = `Applied ${result.changes} change(s) to the live ${state.account} settings.`;
        state.planPreview = null;
        return refreshOverview();
    } else if (action === 'plan-undo') {
        if (!window.confirm(`Undo the last apply to the live ${state.account} settings?`)) return undefined;
        const result = await postJson('/algo-trader/research/plan/rollback', { account: state.account });
        if (result) state.message = `Undid the last apply to the live ${state.account} settings.`;
        state.planPreview = null;
        return refreshOverview();
    } else if (action === 'save-custom') {
        const root = host();
        const name = root?.querySelector('[data-lab-custom-name]')?.value.trim() || '';
        const parameters = Object.fromEntries([...root?.querySelectorAll('[data-lab-custom-param]') || []]
            .map(input => [input.dataset.labCustomParam, input.tagName === 'SELECT' ? input.value : Number(input.value)]));
        const saved = await postJson('/algo-trader/research/profiles', { account: state.account, strategy: state.strategy, name, parameters });
        if (saved) state.message = `Saved profile ${name} for ${state.strategy}.`;
        return refreshOverview();
    }
    if (action === 'cancel' && state.job) {
        const job = await postJson(`/algo-trader/research/jobs/${encodeURIComponent(state.job.id)}/cancel`, {});
        if (job) state.message = 'Cancelling after the current underlying.';
    } else if (action === 'save-profile') {
        const name = state.saveName.trim();
        const tunable = current()?.tunable || [];
        const best = state.results?.results?.find(item => item.final_parameters) || null;
        if (!name) state.error = 'Enter a profile name first.';
        else if (!best) state.error = 'No finished results to save yet.';
        else {
            const parameters = Object.fromEntries(tunable.filter(key => key in best.final_parameters).map(key => [key, best.final_parameters[key]]));
            const saved = await postJson('/algo-trader/research/profiles', { strategy: state.strategy, name, parameters, source_job: state.job?.id || null });
            if (saved) {
                state.message = `Saved profile ${name}.`;
                state.saveName = '';
            }
            return refreshOverview();
        }
    }
    render();
    return undefined;
}

// Mounted once per account. Switching the Equity/Options switch changes the account; the archive refresh does not.
export function mountResearchLab(element, account = 'options') {
    if (!element) return;
    if (!boundHosts.has(element)) {
        boundHosts.add(element);
        element.addEventListener('click', event => {
            const strategyButton = event.target.closest('[data-lab-strategy]');
            if (strategyButton) {
                state.strategy = strategyButton.dataset.labStrategy;
                state.planPreview = null;
                state.saveName = '';
                render();
                return;
            }
            const button = event.target.closest('[data-lab-action]');
            if (!button || !element.contains(button)) return;
            if (button.dataset.labAction === 'toggle-log') {
                state.logOpen = !state.logOpen;
                renderLiveOnly();
                return;
            }
            handleAction(button.dataset.labAction);
        });
        // Enter in a field of the equity form only confirms the field. The run starts with the Start button.
        element.addEventListener('keydown', event => {
            if (event.key === 'Enter' && event.target.matches('[data-lab-equity-field]')) event.preventDefault();
        });
        element.addEventListener('submit', event => {
            if (event.target.matches('[data-lab-form="start-equity"]')) {
                event.preventDefault();
                startEquityRun();
                return;
            }
            if (event.target.matches('[data-lab-form="start"]')) {
                event.preventDefault();
                startRun();
            }
        });
        // Keep what the user typed in state, so a re-render never clears it.
        const capture = event => {
            const target = event.target;
            if (target.matches('[data-lab-results-run]')) {
                selectResults(target.value);
                return;
            }
            if (target.matches('[data-lab-field]')) state.form[target.name] = target.value;
            if (target.matches('[data-lab-pick]')) {
                const id = target.dataset.labPick;
                const list = state.picks[state.account];
                state.picks[state.account] = target.checked ? [...new Set([...list, id])] : list.filter(item => item !== id);
                render();
                return;
            }
            if (target.matches('[data-lab-equity-field]')) state.equity[target.dataset.labEquityField] = target.value;
            if (target.matches('[data-lab-plan]')) {
                state.plan = { ...state.plan, [target.dataset.labPlan]: target.value };
                state.planPreview = null;
                render();
                if (state.overview?.read_only) {
                    state.message = 'Demo: the plan is kept on this page only. It is not saved.';
                    render();
                } else {
                    apiRequest('/algo-trader/research/plan', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ account: state.account, profiles: state.plan }) }, message => { state.error = message; }).then(saved => { if (saved) { state.message = 'Plan saved.'; render(); } });
                }
            }
            if (target.matches('[data-lab-input="saveName"]')) state.saveName = target.value;
        };
        element.addEventListener('input', capture);
        element.addEventListener('change', capture);
        // Native details elements do not bubble toggle events, so listen in the capture phase.
        element.addEventListener('toggle', event => {
            if (event.target.matches?.('[data-lab-rules]')) state.rulesOpen = event.target.open;
        }, true);
    }
    if (element.dataset.account === account && state.overview) return;
    element.dataset.account = account;
    state.account = account;
    state.overview = null;
    state.results = null;
    state.resultsJobId = null;
    state.resultsFor = null;
    state.planPreview = null;
    state.message = '';
    state.error = '';
    refreshOverview();
}
