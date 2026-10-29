import { apiRequest } from '../api.js';
import { escapeHtml } from '../escape.js';

// Interactive options research: start a run, follow its log, read results, and promote a profile with rollback.
const HOST_ID = 'research-lab-host';
const POLL_MS = 2000;
const LOG_TAIL_LINES = 200;

const state = {
    overview: null,
    strategy: 'vrp',
    form: { underlyings: '', start: '', end: '', profile: '' },
    job: null,
    results: null,
    logText: '',
    logOffset: 0,
    timer: null,
    message: '',
    error: '',
    saveName: '',
    preview: null,
};
const boundHosts = new WeakSet();

function host() {
    return document.getElementById(HOST_ID);
}

function tail(text) {
    return String(text || '').split('\n').slice(-LOG_TAIL_LINES).join('\n');
}

function percent(job) {
    const total = job?.progress?.total || 0;
    const done = job?.progress?.done || 0;
    return total ? Math.round((done / total) * 100) : 0;
}

function isRunning(job) {
    return job?.state === 'running' || job?.state === 'cancelling';
}

function tunableFor(strategy) {
    return state.overview?.options.find(item => item.id === strategy)?.tunable || [];
}

function renderParameterTable(parameters) {
    const rows = Object.entries(parameters || {})
        .map(([key, value]) => `<tr><th>${escapeHtml(key)}</th><td>${escapeHtml(JSON.stringify(value))}</td></tr>`)
        .join('');
    return rows ? `<table class="lab-table"><tbody>${rows}</tbody></table>` : '<p class="lab-muted">No live parameters saved for this strategy.</p>';
}

function renderStrategyTabs() {
    const tabs = state.overview.options.map(item => `<button type="button" class="lab-tab ${item.id === state.strategy ? 'active' : ''}" data-lab-strategy="${escapeHtml(item.id)}">${escapeHtml(item.id.replaceAll('_', ' '))}</button>`).join('');
    return `<div class="lab-tabs" role="tablist">${tabs}</div>`;
}

function renderLive() {
    const job = state.job;
    if (!job) return '<p class="lab-muted">No run in this session yet.</p>';
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
        <pre class="lab-log" aria-label="Live research log">${escapeHtml(tail(state.logText) || 'Waiting for output…')}</pre>
    </div>`;
}

function renderResults() {
    const data = state.results;
    if (!data?.results?.length) return '<p class="lab-muted">Results appear here as each underlying finishes.</p>';
    const tunable = tunableFor(data.strategy);
    const rows = data.results.map(item => {
        if (item.status === 'error') {
            return `<tr><td>${escapeHtml(item.underlying)}</td><td colspan="4" class="lab-bad">${escapeHtml(item.error || 'error')}</td></tr>`;
        }
        const sharpe = item.oos_metrics?.annualized_sharpe;
        const params = Object.fromEntries(tunable.filter(key => key in (item.final_parameters || {})).map(key => [key, item.final_parameters[key]]));
        return `<tr><td>${escapeHtml(item.underlying)}</td><td>${escapeHtml(item.status)}${item.approved ? ' · approved' : ''}</td>
            <td>${typeof sharpe === 'number' ? escapeHtml(sharpe.toFixed(2)) : '—'}</td><td>${item.observed_oos_trades ?? '—'}</td>
            <td><code>${escapeHtml(JSON.stringify(params))}</code></td></tr>`;
    }).join('');
    return `<table class="lab-table lab-results"><thead><tr><th>Underlying</th><th>Status</th><th>OOS Sharpe</th><th>OOS trades</th><th>Final tunable parameters</th></tr></thead><tbody>${rows}</tbody></table>
        <div class="lab-save"><input type="text" data-lab-input="saveName" placeholder="profile name" value="${escapeHtml(state.saveName)}" maxlength="40">
        <button type="button" class="btn btn-secondary" data-lab-action="save-profile">Save first approved-or-finished parameters as profile</button></div>`;
}

function renderStartForm() {
    const profiles = state.overview.options.find(item => item.id === state.strategy)?.profiles || [];
    const options = ['<option value="">Live settings (no profile)</option>']
        .concat(profiles.map(item => `<option value="${escapeHtml(item.name)}" ${state.form.profile === item.name ? 'selected' : ''}>${escapeHtml(item.name)}</option>`))
        .join('');
    const busy = Boolean(state.overview.active_job);
    return `<form class="lab-form" data-lab-form="start">
        <label>Underlyings (max ${state.overview.limits.maxUnderlyings}, comma separated)
            <input type="text" name="underlyings" value="${escapeHtml(state.form.underlyings)}" placeholder="SPY, QQQ, GLD" required></label>
        <label>Start <input type="date" name="start" value="${escapeHtml(state.form.start)}" min="${escapeHtml(state.overview.limits.earliestStart)}"></label>
        <label>End <input type="date" name="end" value="${escapeHtml(state.form.end)}"></label>
        <label>Profile <select name="profile">${options}</select></label>
        <button type="submit" class="btn btn-primary" ${busy ? 'disabled' : ''}>Start research</button>
        ${busy ? `<p class="lab-muted">Run ${escapeHtml(state.overview.active_job)} is already running.</p>` : ''}
    </form>`;
}

function renderPromotion() {
    const strategy = state.overview.options.find(item => item.id === state.strategy);
    const profiles = strategy?.profiles || [];
    const choices = profiles.map(item => `<option value="${escapeHtml(item.name)}">${escapeHtml(item.name)}</option>`).join('');
    const history = (strategy?.history || []).slice().reverse()
        .map(item => `<li>${escapeHtml(item.at)} · ${escapeHtml(item.action)}${item.profile ? ` · ${escapeHtml(item.profile)}` : ''}${item.restored_entry ? ` · restored ${escapeHtml(item.restored_entry)}` : ''}</li>`)
        .join('');
    const preview = state.preview;
    const diff = preview ? `<table class="lab-table"><thead><tr><th>Parameter</th><th>Live now</th><th>Profile</th></tr></thead><tbody>${preview.diff
        .map(item => `<tr class="${item.changed ? 'lab-changed' : ''}"><th>${escapeHtml(item.key)}</th><td>${escapeHtml(JSON.stringify(item.current))}</td><td>${escapeHtml(JSON.stringify(item.proposed))}</td></tr>`)
        .join('')}</tbody></table>
        <p class="lab-muted">${preview.changes} change(s). Applies to the next options paper cycle.</p>
        <button type="button" class="btn btn-primary" data-lab-action="confirm-promote">Confirm promotion</button>` : '';
    return `<div class="lab-promote">
        <label>Profile <select data-lab-input="promoteProfile">${choices || '<option value="">No profiles yet</option>'}</select></label>
        <button type="button" class="btn btn-secondary" data-lab-action="preview-promote" ${profiles.length ? '' : 'disabled'}>Preview changes</button>
        <button type="button" class="btn btn-secondary" data-lab-action="rollback">Roll back last promotion</button>
        ${diff}
        <h4>History</h4><ul class="lab-history">${history || '<li class="lab-muted">No promotions yet.</li>'}</ul>
    </div>`;
}

function renderLab() {
    if (!state.overview) return '<section class="card algo-panel lab-panel"><p class="lab-muted">Loading research lab…</p></section>';
    const notice = [
        state.message ? `<p class="lab-ok">${escapeHtml(state.message)}</p>` : '',
        state.error ? `<p class="lab-bad">${escapeHtml(state.error)}</p>` : '',
    ].join('');
    const strategy = state.overview.options.find(item => item.id === state.strategy);
    return `<section class="card algo-panel lab-panel" id="research-lab">
        <div class="algo-section-heading"><div><span class="algo-eyebrow">RESEARCH LAB</span><h2>Interactive options research</h2>
        <p>One job at a time, on the shared universe. Lab runs never change the live champion; promotion is a separate, confirmed step.</p></div></div>
        ${notice}
        ${renderStrategyTabs()}
        <div class="lab-grid">
            <div><h3>Live parameters</h3>${strategy ? renderParameterTable(strategy.parameters) : ''}</div>
            <div><h3>Start a run</h3>${renderStartForm()}</div>
        </div>
        <h3>Progress and log</h3><div id="lab-live">${renderLive()}</div>
        <h3>Results</h3>${renderResults()}
        <h3>Promotion</h3>${renderPromotion()}
    </section>`;
}

function render() {
    const element = host();
    if (element) element.innerHTML = renderLab();
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
    const overview = await apiRequest('/api/algo-trader/research/lab', {}, message => { state.error = message; });
    if (overview) state.overview = overview;
    if (overview?.active_job && !state.job) {
        const job = await apiRequest(`/api/algo-trader/research/jobs/${encodeURIComponent(overview.active_job)}`);
        if (job) {
            state.job = job;
            state.logText = '';
            state.logOffset = 0;
        }
    }
    if (isRunning(state.job)) startPolling();
    render();
}

async function pollJob() {
    const id = state.job?.id;
    if (!id) return stopPolling();
    const job = await apiRequest(`/api/algo-trader/research/jobs/${encodeURIComponent(id)}`);
    if (!job) return;
    state.job = job;
    const chunk = await apiRequest(`/api/algo-trader/research/jobs/${encodeURIComponent(id)}/log?offset=${state.logOffset}`);
    if (chunk) {
        state.logText += chunk.text;
        state.logOffset = chunk.next;
    }
    if (!isRunning(job)) {
        stopPolling();
        state.results = await apiRequest(`/api/algo-trader/research/jobs/${encodeURIComponent(id)}/results`);
        state.message = `Run ${job.state}.`;
        return refreshOverview();
    }
    // Only the live panel updates while a run is going, so the form and any typing are untouched.
    const live = document.getElementById('lab-live');
    if (live) live.innerHTML = renderLive();
    return undefined;
}

async function postJson(url, body) {
    return apiRequest(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    }, message => { state.error = message; });
}

async function startRun(form) {
    const data = new FormData(form);
    state.form = {
        underlyings: String(data.get('underlyings') || ''),
        start: String(data.get('start') || ''),
        end: String(data.get('end') || ''),
        profile: String(data.get('profile') || ''),
    };
    state.error = '';
    const job = await postJson('/api/algo-trader/research/jobs', {
        account: 'options',
        strategy: state.strategy,
        underlyings: state.form.underlyings.split(/[\s,]+/).filter(Boolean),
        start: state.form.start || null,
        end: state.form.end || null,
        profile: state.form.profile || null,
    });
    if (job) {
        state.job = job;
        state.results = null;
        state.logText = '';
        state.logOffset = 0;
        state.message = `Started ${job.id}.`;
        startPolling();
    }
    render();
}

async function handleAction(action) {
    state.error = '';
    if (action === 'cancel' && state.job) {
        const job = await postJson(`/api/algo-trader/research/jobs/${encodeURIComponent(state.job.id)}/cancel`, {});
        if (job) state.message = 'Cancelling after the current underlying.';
    } else if (action === 'save-profile') {
        const name = state.saveName.trim();
        const tunable = tunableFor(state.strategy);
        const best = state.results?.results?.find(item => item.final_parameters) || null;
        if (!name) state.error = 'Enter a profile name first.';
        else if (!best) state.error = 'No finished results to save yet.';
        else {
            const parameters = Object.fromEntries(tunable.filter(key => key in best.final_parameters).map(key => [key, best.final_parameters[key]]));
            const saved = await postJson('/api/algo-trader/research/profiles', { strategy: state.strategy, name, parameters, source_job: state.job?.id || null });
            if (saved) {
                state.message = `Saved profile ${name}.`;
                state.saveName = '';
            }
            return refreshOverview();
        }
    } else if (action === 'preview-promote') {
        const profile = host()?.querySelector('[data-lab-input="promoteProfile"]')?.value;
        state.preview = await postJson('/api/algo-trader/research/promote', { account: 'options', strategy: state.strategy, profile });
    } else if (action === 'confirm-promote' && state.preview) {
        const result = await postJson('/api/algo-trader/research/promote', {
            account: 'options', strategy: state.strategy, profile: state.preview.profile, confirm: true,
        });
        if (result) state.message = `Promoted ${state.preview.profile} to live ${state.strategy} (${result.applied}).`;
        state.preview = null;
        return refreshOverview();
    } else if (action === 'rollback') {
        const result = await postJson('/api/algo-trader/research/rollback', { strategy: state.strategy });
        if (result) state.message = `Rolled back ${state.strategy} (restored ${result.restored}).`;
        state.preview = null;
        return refreshOverview();
    }
    render();
    return undefined;
}

// Bind once per host element. Handlers look up the current host so a page re-render never leaves a stale timer or listener.
export function mountResearchLab(element) {
    if (!element) return;
    if (!boundHosts.has(element)) {
        boundHosts.add(element);
        element.addEventListener('click', event => {
            const strategyButton = event.target.closest('[data-lab-strategy]');
            if (strategyButton) {
                state.strategy = strategyButton.dataset.labStrategy;
                state.preview = null;
                state.saveName = '';
                render();
                return;
            }
            const button = event.target.closest('[data-lab-action]');
            if (button && element.contains(button)) handleAction(button.dataset.labAction);
        });
        element.addEventListener('submit', event => {
            if (event.target.matches('[data-lab-form="start"]')) {
                event.preventDefault();
                startRun(event.target);
            }
        });
        element.addEventListener('input', event => {
            if (event.target.matches('[data-lab-input="saveName"]')) state.saveName = event.target.value;
        });
    }
    refreshOverview();
}

export { HOST_ID as RESEARCH_LAB_HOST_ID };
