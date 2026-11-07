import { apiRequest } from '../api.js';
import { escapeHtml } from '../escape.js';

// Read-only status of the scheduled data jobs: the ThetaData history pipeline and the cron logs.
// The research view replaces its DOM on each refresh, so the panel mounts again each time and reloads itself.
const REFRESH_MS = 60_000;
const PHASE_LABELS = {
    not_started: 'Not started',
    eod_recent: 'Last 20 sessions',
    eod: 'Full EOD archive',
    opening: 'Opening archive',
    complete: 'Complete',
};

let host = null;
let timer = null;
// The last good render, so a remount or a failed refresh never blanks the panel.
let lastHtml = '';
let lastStatus = null;
// Job names the user has expanded.
const openLogs = new Set();
const boundHosts = new WeakSet();

function bytes(value) {
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
    return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function stamp(value) {
    return value ? escapeHtml(new Date(value).toLocaleString()) : '—';
}

function thetaBlock(theta) {
    const total = theta.symbols || 0;
    const action = theta.action
        ? `<p class="data-status-action"><strong>${escapeHtml(theta.action)}</strong></p>`
        : '';
    const error = theta.last_error
        ? `<p class="data-status-error">Last error: ${escapeHtml(theta.last_error)}</p>`
        : '';
    const notice = theta.notify_pending
        ? '<p class="data-status-error">The completion message did not reach Telegram. It will be retried on the next run.</p>'
        : '';
    return `
        <h3>ThetaData history backfill</h3>
        <p>Phase: <strong>${escapeHtml(PHASE_LABELS[theta.phase] || theta.phase)}</strong>${theta.end_date ? ` · archives through ${escapeHtml(theta.end_date)}` : ''} · updated ${stamp(theta.updated_at)}</p>
        <table class="data-status-table">
            <tbody>
                <tr><th>Last 20 sessions for all symbols</th><td>${stamp(theta.recent_pass_complete_at)}</td></tr>
                <tr><th>EOD archives complete</th><td>${theta.eod_complete} of ${total}</td></tr>
                <tr><th>Opening archives complete</th><td>${theta.opening_complete} of ${total}</td></tr>
                <tr><th>Finished</th><td>${stamp(theta.completed_at)}</td></tr>
                <tr><th>THETA_DATA_API_KEY in .env</th><td>${theta.key_present ? 'present' : 'absent'}</td></tr>
            </tbody>
        </table>
        ${action}${error}${notice}`;
}

function logRow(log, open) {
    const failed = Boolean(log.last_error);
    const detail = open ? `
        <dl class="data-status-log-detail">
            <div><dt>Size</dt><dd>${escapeHtml(bytes(log.size_bytes))}</dd></div>
            <div><dt>Last written</dt><dd>${stamp(log.modified_at)}</dd></div>
            ${failed ? `<div><dt>Last error</dt><dd class="data-status-error">${escapeHtml(log.last_error)}</dd></div>` : ''}
            ${log.last_line ? `<div><dt>Last line</dt><dd>${escapeHtml(log.last_line)}</dd></div>` : ''}
        </dl>` : '';
    return `
        <li class="data-status-log-item">
            <button type="button" class="data-status-log-button" data-log-toggle="${escapeHtml(log.name)}" aria-expanded="${open}">
                <span class="data-status-log-name ${failed ? 'data-status-error' : ''}">${escapeHtml(log.name)}</span>
                <span class="data-status-log-time">${stamp(log.modified_at)}</span>
                <span class="data-status-log-caret" aria-hidden="true">${open ? '▴' : '▾'}</span>
            </button>${detail}
        </li>`;
}

function logsBlock(logs, openLogs) {
    if (!logs.length) return '<p>No job logs yet.</p>';
    return `
        <h3>Scheduled job logs</h3>
        <p class="data-status-log-hint">Tap a job for details.</p>
        <ul class="data-status-logs">${logs.map(log => logRow(log, openLogs.has(log.name))).join('')}</ul>`;
}

export function renderDataStatus(status, openLogs = new Set()) {
    if (status.demo_mode) return '<p>Scheduled data jobs are not run in demo mode.</p>';
    return `${thetaBlock(status.theta)}${logsBlock(status.logs || [], openLogs)}`;
}

function paint(status) {
    lastStatus = status;
    const html = renderDataStatus(status, openLogs);
    if (html === lastHtml) return;
    lastHtml = html;
    host.innerHTML = html;
}

async function load() {
    const status = await apiRequest('/algo-trader/research/data-status', {}, message => {
        // Keep the last good status on screen; only show the error if there is nothing to keep.
        if (host?.isConnected && !lastHtml) host.innerHTML = `<p class="data-status-error">${escapeHtml(message)}</p>`;
    });
    if (!status || !host?.isConnected) return;
    paint(status);
}

export function mountDataStatus(element) {
    host = element;
    if (!boundHosts.has(element)) {
        boundHosts.add(element);
        // Tapping a job toggles its details. The open set survives the 60-second refresh.
        element.addEventListener('click', event => {
            const button = event.target.closest('[data-log-toggle]');
            if (!button || !element.contains(button) || !lastStatus) return;
            const name = button.dataset.logToggle;
            if (openLogs.has(name)) openLogs.delete(name);
            else openLogs.add(name);
            host = element;
            paint(lastStatus);
        });
    }
    host.innerHTML = lastHtml || '<p class="data-status-loading">Loading scheduled job status…</p>';
    load();
    if (!timer) {
        timer = setInterval(() => {
            if (host?.isConnected) load();
        }, REFRESH_MS);
    }
}
