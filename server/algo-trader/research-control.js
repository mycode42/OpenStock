'use strict';

// Pause or enable the nightly research timers, and stop research that is running now.
// The nightly jobs are user units (equity-research.timer, options-research.timer), so systemctl --user controls them.
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');

const execFileAsync = promisify(execFile);

const NIGHTLY = Object.freeze({
    equity: { label: 'Equity research', timer: 'equity-research.timer', service: 'equity-research.service', when: '20:00 ET daily' },
    options: { label: 'Options research', timer: 'options-research.timer', service: 'options-research.service', when: '17:00 ET daily' },
});
// Research processes started outside the timers (for example a manual run or a lab run).
const RESEARCH_PROCESS = /research\.equity|research\.options|equity_research\.auto_research|options\.research\.lab_run/;

function defaultRunner(file, args) {
    return execFileAsync(file, args, { timeout: 20_000 }).then(result => result.stdout, error => {
        // pgrep exits 1 when nothing matches; that is an answer, not a failure.
        if (file === 'pgrep' && error.code === 1) return '';
        throw error;
    });
}

function systemctl(runner, args) {
    return runner('systemctl', ['--user', ...args]);
}

function parseShow(text) {
    return Object.fromEntries(String(text).split('\n').filter(Boolean).map(line => {
        const index = line.indexOf('=');
        return [line.slice(0, index), line.slice(index + 1)];
    }));
}

function nightlyKey(name) {
    if (!Object.prototype.hasOwnProperty.call(NIGHTLY, name)) {
        const error = new Error(`unknown nightly job: ${name}`);
        error.status = 400;
        throw error;
    }
    return name;
}

async function nightlyState(runner, key) {
    const job = NIGHTLY[key];
    const timer = parseShow(await systemctl(runner, ['show', job.timer, '-p', 'UnitFileState', '-p', 'ActiveState']));
    const service = parseShow(await systemctl(runner, ['show', job.service, '-p', 'ActiveState']));
    return {
        key, label: job.label, when: job.when,
        enabled: timer.UnitFileState === 'enabled',
        running: ['active', 'activating'].includes(service.ActiveState),
    };
}

// systemctl --user fails when there is no user session (containers, SSH without lingering). Then no user timers run here.
function hasNoUserSession(error) {
    return /Failed to connect to|No medium found|Transport endpoint|not been booted with systemd/i.test(`${error?.stderr || ''} ${error?.message || ''}`);
}

async function researchState({ runner = defaultRunner } = {}) {
    try {
        const nightly = await Promise.all(Object.keys(NIGHTLY).map(key => nightlyState(runner, key)));
        return { available: true, nightly, running: nightly.some(job => job.running) };
    } catch (error) {
        if (!hasNoUserSession(error) && error?.code !== 'ENOENT') throw error;
        return { available: false, nightly: [], running: false, reason: 'No systemd user session on this host, so the nightly timers are not managed here.' };
    }
}

async function researchPythonPids(runner, pids) {
    if (!pids.length) return [];
    const listed = String(await runner('ps', ['-o', 'pid=,comm=', '-p', pids.join(',')]));
    return listed.split('\n').map(line => line.trim().split(/\s+/)).filter(([, command]) => /^python/.test(command || '')).map(([pid]) => Number(pid));
}

async function killRunningResearch({ runner = defaultRunner, ownPid = process.pid, killImpl = (pid, signal) => process.kill(pid, signal) } = {}) {
    const stopped = [];
    for (const key of Object.keys(NIGHTLY)) {
        const state = await nightlyState(runner, key);
        if (state.running) {
            await systemctl(runner, ['stop', NIGHTLY[key].service]);
            stopped.push(NIGHTLY[key].service);
        }
    }
    // Manual and lab runs are not in a unit, so find them by their command line, for this user only.
    const uid = String(process.getuid ? process.getuid() : '');
    const listed = await runner('pgrep', ['-u', uid, '-f', RESEARCH_PROCESS.source]);
    const candidates = String(listed).split('\n').map(line => Number(line.trim())).filter(pid => pid > 0 && pid !== ownPid);
    // A shell or editor can mention a research command in its arguments. Only Python processes are research runs.
    const pids = await researchPythonPids(runner, candidates);
    for (const pid of pids) {
        try { killImpl(pid, 'SIGTERM'); } catch { /* already gone */ }
    }
    return { stopped_services: stopped, terminated_pids: pids };
}

// The demo shows the same switches with sample states. Nothing is changed in demo mode.
function demoState() {
    const nightly = Object.keys(NIGHTLY).map(key => ({
        key, label: NIGHTLY[key].label, when: NIGHTLY[key].when, enabled: true, running: false,
    }));
    return { available: false, nightly, running: false };
}

module.exports = { hasNoUserSession, NIGHTLY, RESEARCH_PROCESS, researchState, killRunningResearch, nightlyKey, demoState };
