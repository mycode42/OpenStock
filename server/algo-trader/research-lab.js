const fs = require('node:fs');
const fsp = require('node:fs/promises');
const path = require('node:path');
const { randomUUID } = require('node:crypto');
const { spawn } = require('node:child_process');
const { readJson } = require('./io');
const { loadOptionsSettings, mergeConfig, optionsConfigPath, validateConfigShape, validateOptionsConfig } = require('./settings');

const LIMITS = Object.freeze({
    maxUnderlyings: 20,
    minFreeMemoryMb: 6144,
    earliestStart: '2020-01-01',
});
const OPTIONS_STRATEGIES = Object.freeze(['vrp', 'long_volatility']);
const EQUITY_STRATEGIES = Object.freeze([
    'swing_trend_following', 'swing_momentum', 'swing_mean_reversion', 'swing_multi_factor',
    'intraday_mean_reversion', 'intraday_trend_following', 'intraday_momentum',
]);
const EVENT_PREFIX = 'LAB_EVENT ';
const LOG_CHUNK_BYTES = 64 * 1024;
const TICKER_PATTERN = /^[A-Z][A-Z0-9.]{0,9}$/;
const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;
const PROFILE_NAME_PATTERN = /^[a-z0-9][a-z0-9_-]{0,39}$/i;

class LabError extends Error {
    constructor(message, status = 400) {
        super(message);
        this.status = status;
    }
}

function isPlainObject(value) {
    return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function isIsoDate(value) {
    if (!DATE_PATTERN.test(value)) return false;
    const parsed = new Date(`${value}T00:00:00Z`);
    return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}

// Validates a start-run request. Returns the normalized job request or throws LabError.
function validateJobRequest(body, { today = new Date().toISOString().slice(0, 10) } = {}) {
    if (!isPlainObject(body)) throw new LabError('request body must be an object');
    if (body.account !== 'options') throw new LabError('only options research can be started from the lab yet', 409);
    if (!OPTIONS_STRATEGIES.includes(body.strategy)) {
        throw new LabError(`strategy must be one of ${OPTIONS_STRATEGIES.join(', ')}`);
    }
    if (!Array.isArray(body.underlyings) || body.underlyings.length === 0) {
        throw new LabError('underlyings must be a non-empty list');
    }
    const underlyings = [...new Set(body.underlyings.map(item => String(item).trim().toUpperCase()))];
    if (underlyings.some(item => !TICKER_PATTERN.test(item))) throw new LabError('every underlying must be a ticker symbol');
    if (underlyings.length > LIMITS.maxUnderlyings) {
        throw new LabError(`at most ${LIMITS.maxUnderlyings} underlyings per run`);
    }
    const start = body.start ? String(body.start) : null;
    const end = body.end ? String(body.end) : null;
    if (start && (!isIsoDate(start) || start < LIMITS.earliestStart)) {
        throw new LabError(`start must be a date on or after ${LIMITS.earliestStart}`);
    }
    if (end && (!isIsoDate(end) || end > today)) throw new LabError('end must be a date no later than today');
    if (start && end && start >= end) throw new LabError('start must be before end');
    const profile = body.profile ? String(body.profile) : null;
    if (profile && !PROFILE_NAME_PATTERN.test(profile)) throw new LabError('profile name is not valid');
    return { account: 'options', strategy: body.strategy, underlyings, start, end, profile };
}

// Only the grid parameters the research search may vary are editable through profiles.
function tunableKeys(baseConfig, strategy) {
    const grid = baseConfig?.search?.grids?.[strategy];
    if (!isPlainObject(grid)) throw new LabError(`no search grid is configured for ${strategy}`, 500);
    return Object.keys(grid).sort();
}

function validateParameters(parameters, allowedKeys) {
    if (!isPlainObject(parameters)) throw new LabError('parameters must be an object');
    const clean = {};
    for (const [key, value] of Object.entries(parameters)) {
        if (!allowedKeys.includes(key)) throw new LabError(`${key} is not a tunable parameter for this strategy`);
        const scalar = typeof value === 'boolean' || (typeof value === 'number' && Number.isFinite(value)) || typeof value === 'string';
        const list = Array.isArray(value) && value.every(item => typeof item === 'string');
        if (!scalar && !list) throw new LabError(`${key} must be a number, boolean, string, or list of strings`);
        clean[key] = value;
    }
    if (Object.keys(clean).length === 0) throw new LabError('a profile needs at least one parameter');
    return clean;
}

// Research baseline for one job: the profile replaces the strategy's default section, and lab runs never write champion state.
function buildJobConfig(baseConfig, { strategy, parameters, start, end }) {
    const config = JSON.parse(JSON.stringify(baseConfig));
    config.champion = { ...(config.champion || {}), enabled: false };
    config.strategy_defaults = {
        ...(config.strategy_defaults || {}),
        [strategy]: { ...((config.strategy_defaults || {})[strategy] || {}), ...(parameters || {}) },
    };
    if (start || end) config.date_range = { start, end };
    return config;
}

function diffParameters(current, proposed) {
    return Object.keys(proposed).sort().map(key => ({
        key,
        current: current[key] === undefined ? null : current[key],
        proposed: proposed[key],
        changed: JSON.stringify(current[key]) !== JSON.stringify(proposed[key]),
    }));
}

// /proc/meminfo reports MemAvailable in kB; the lab needs the value in MB.
function parseFreeMemoryMb(meminfoText) {
    const match = /^MemAvailable:\s+(\d+)\s+kB/m.exec(meminfoText);
    if (!match) throw new LabError('free memory is unavailable', 503);
    return Math.floor(Number(match[1]) / 1024);
}

function parseLabEvents(logText) {
    const events = [];
    for (const line of logText.split('\n')) {
        if (!line.startsWith(EVENT_PREFIX)) continue;
        try { events.push(JSON.parse(line.slice(EVENT_PREFIX.length))); } catch { /* a partially written line is read on the next poll */ }
    }
    return events;
}

function summarizeProgress(events) {
    const done = events.filter(item => item.event === 'underlying_done' || item.event === 'underlying_error');
    const started = events.filter(item => item.event === 'underlying_started').pop();
    const finished = events.find(item => item.event === 'finished');
    const started_run = events.find(item => item.event === 'started');
    const total = started_run?.total ?? started?.total ?? 0;
    return {
        total,
        done: done.length,
        failures: events.filter(item => item.event === 'underlying_error').length,
        current: finished ? null : (started && !done.some(item => item.underlying === started.underlying) ? started.underlying : null),
        approved: events.filter(item => item.event === 'underlying_done' && item.approved).map(item => item.underlying),
        finished: Boolean(finished),
    };
}

function pickNumericMetrics(source) {
    if (!isPlainObject(source)) return {};
    const out = {};
    for (const [key, value] of Object.entries(source)) {
        if (typeof value === 'number' && Number.isFinite(value)) out[key] = value;
    }
    return out;
}

async function readJsonFile(file, fallback) {
    try {
        return JSON.parse(await fsp.readFile(file, 'utf8'));
    } catch (error) {
        if (error.code === 'ENOENT') return fallback;
        throw error;
    }
}

async function writeJsonAtomic(file, value) {
    await fsp.mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
    const temporary = `${file}.${process.pid}.${randomUUID()}.tmp`;
    const handle = await fsp.open(temporary, 'wx', 0o600);
    try {
        await handle.writeFile(`${JSON.stringify(value, null, 2)}\n`, 'utf8');
        await handle.sync();
    } finally {
        await handle.close();
    }
    await fsp.rename(temporary, file);
}

function processAlive(pid) {
    if (!pid) return false;
    try {
        process.kill(pid, 0);
        return true;
    } catch (error) {
        return error.code === 'EPERM';
    }
}

class ResearchLab {
    constructor({ root, python = process.env.ALGO_TRADER_PYTHON, now = () => new Date(), spawnImpl = spawn, meminfoPath = '/proc/meminfo' }) {
        this.root = root;
        this.labDir = path.join(root, 'data', 'research-lab');
        this.python = python;
        this.now = now;
        this.spawnImpl = spawnImpl;
        this.meminfoPath = meminfoPath;
        this.children = new Map();
    }

    // Same layering as options/research/runner.py: research defaults, then the portfolio override.
    async baseConfig() {
        const defaults = await readJson(path.join(this.root, 'options', 'research', 'config.default.json'));
        const override = await readJson(path.join(this.root, 'options', 'research', 'config.portfolio.json'));
        return mergeConfig(defaults, override);
    }

    jobDir(id) {
        if (!/^[A-Za-z0-9T:._-]+$/.test(id)) throw new LabError('job id is not valid', 404);
        return path.join(this.labDir, 'jobs', id);
    }

    async readJob(id) {
        const job = await readJsonFile(path.join(this.jobDir(id), 'job.json'), null);
        if (!job) throw new LabError('job was not found', 404);
        if (job.state === 'running' && !this.children.has(id) && !processAlive(job.pid)) {
            job.state = 'interrupted';
            await writeJsonAtomic(path.join(this.jobDir(id), 'job.json'), job);
        }
        return job;
    }

    async freeMemoryMb() {
        return parseFreeMemoryMb(await fsp.readFile(this.meminfoPath, 'utf8'));
    }

    activeJob() {
        return [...this.children.keys()][0] || null;
    }

    async startJob(body) {
        const request = validateJobRequest(body, { today: this.now().toISOString().slice(0, 10) });
        if (this.activeJob()) throw new LabError(`research job ${this.activeJob()} is already running`, 409);
        const freeMb = await this.freeMemoryMb();
        if (freeMb < LIMITS.minFreeMemoryMb) {
            throw new LabError(`only ${freeMb} MB of memory is free; a run needs ${LIMITS.minFreeMemoryMb} MB`, 503);
        }
        const baseConfig = await this.baseConfig();
        const allowed = tunableKeys(baseConfig, request.strategy);
        const parameters = request.profile ? await this.profileParameters(request.strategy, request.profile) : {};
        if (request.profile) validateParameters(parameters, allowed);

        const id = `${this.now().toISOString().replace(/[:.]/g, '-')}-${randomUUID().slice(0, 8)}`;
        const dir = this.jobDir(id);
        await fsp.mkdir(dir, { recursive: true, mode: 0o700 });
        const configPath = path.join(dir, 'config.json');
        await writeJsonAtomic(configPath, buildJobConfig(baseConfig, { strategy: request.strategy, parameters, ...request }));
        const logPath = path.join(dir, 'log.txt');
        const logFd = fs.openSync(logPath, 'a');
        const args = ['-m', 'options.research.lab_run', '--config', configPath, '--strategy', request.strategy,
            '--underlyings', request.underlyings.join(','), '--workers', '8'];
        const configured = this.python;
        const executable = configured || '/usr/local/bin/poetry';
        const command = configured ? args : ['run', 'python', ...args];
        const child = this.spawnImpl(executable, command, {
            cwd: this.root,
            env: { ...process.env },
            detached: true,
            stdio: ['ignore', logFd, logFd],
        });
        fs.closeSync(logFd);
        const job = {
            id, account: 'options', strategy: request.strategy, underlyings: request.underlyings,
            start: request.start, end: request.end, profile: request.profile,
            state: 'running', pid: child.pid, created_at: this.now().toISOString(), updated_at: this.now().toISOString(),
            cancel_requested: false,
        };
        await writeJsonAtomic(path.join(dir, 'job.json'), job);
        this.children.set(id, child);
        child.on('error', error => this.finishJob(id, { state: 'failed', error: error.message }));
        child.on('exit', (code, signal) => {
            const state = job.cancel_requested ? 'cancelled' : (code === 0 ? 'finished' : 'failed');
            this.finishJob(id, { state, exit_code: code, signal: signal || null });
        });
        return job;
    }

    async finishJob(id, patch) {
        this.children.delete(id);
        try {
            const job = await readJsonFile(path.join(this.jobDir(id), 'job.json'), null);
            if (!job) return;
            await writeJsonAtomic(path.join(this.jobDir(id), 'job.json'), { ...job, ...patch, pid: null, updated_at: this.now().toISOString() });
        } catch (error) {
            console.error(`Research lab could not record job ${id}: ${error.message}`);
        }
    }

    async cancelJob(id) {
        const job = await this.readJob(id);
        const child = this.children.get(id);
        if (!child || job.state !== 'running') throw new LabError('job is not running', 409);
        job.cancel_requested = true;
        await writeJsonAtomic(path.join(this.jobDir(id), 'job.json'), job);
        process.kill(-child.pid, 'SIGTERM');
        return job;
    }

    async jobStatus(id) {
        const job = await this.readJob(id);
        const log = await this.readLogText(id);
        const events = parseLabEvents(log.text);
        return { ...job, progress: summarizeProgress(events), log_bytes: log.size };
    }

    async readLogText(id) {
        const file = path.join(this.jobDir(id), 'log.txt');
        try {
            const stats = await fsp.stat(file);
            return { text: await fsp.readFile(file, 'utf8'), size: stats.size };
        } catch (error) {
            if (error.code === 'ENOENT') return { text: '', size: 0 };
            throw error;
        }
    }

    async readLogChunk(id, offset = 0) {
        await this.readJob(id);
        const file = path.join(this.jobDir(id), 'log.txt');
        let size = 0;
        try { size = (await fsp.stat(file)).size; } catch (error) { if (error.code !== 'ENOENT') throw error; }
        const start = Math.max(0, Math.min(Number(offset) || 0, size));
        const length = Math.min(LOG_CHUNK_BYTES, size - start);
        if (length <= 0) return { offset: start, next: start, text: '', size };
        const handle = await fsp.open(file, 'r');
        try {
            const buffer = Buffer.alloc(length);
            await handle.read(buffer, 0, length, start);
            return { offset: start, next: start + length, text: buffer.toString('utf8'), size };
        } finally {
            await handle.close();
        }
    }

    async jobResults(id) {
        const job = await this.readJob(id);
        const log = await this.readLogText(id);
        const done = parseLabEvents(log.text).filter(item => item.event === 'underlying_done' || item.event === 'underlying_error');
        const results = [];
        for (const item of done) {
            if (item.event === 'underlying_error') {
                results.push({ underlying: item.underlying, status: 'error', error: item.failure });
                continue;
            }
            const report = await readJsonFile(path.join(item.run_dir || '', 'report.json'), null);
            const wf = report?.walk_forward || {};
            results.push({
                underlying: item.underlying,
                status: item.status,
                approved: item.approved,
                run_id: item.run_id,
                final_parameters: report?.final_parameters ?? null,
                oos_metrics: pickNumericMetrics(wf.oos_metrics),
                observed_oos_trades: report?.validation?.observed_oos_trades ?? null,
                minimum_oos_trades_met: report?.validation?.minimum_oos_trades_met ?? null,
                parameter_stability_score: report?.validation?.parameter_stability_score ?? null,
            });
        }
        return { job: job.id, strategy: job.strategy, state: job.state, results };
    }

    async listProfiles(strategy) {
        const file = path.join(this.labDir, 'profiles', `${strategy}.json`);
        const data = await readJsonFile(file, { profiles: [] });
        return data.profiles;
    }

    async profileParameters(strategy, name) {
        const profile = (await this.listProfiles(strategy)).find(item => item.name === name);
        if (!profile) throw new LabError(`profile ${name} was not found`, 404);
        return profile.parameters;
    }

    async saveProfile({ strategy, name, parameters, source_job = null }) {
        if (!OPTIONS_STRATEGIES.includes(strategy)) throw new LabError('strategy must be vrp or long_volatility');
        if (!PROFILE_NAME_PATTERN.test(name || '')) throw new LabError('profile name must be letters, digits, dashes or underscores');
        const allowed = tunableKeys(await this.baseConfig(), strategy);
        const clean = validateParameters(parameters, allowed);
        const file = path.join(this.labDir, 'profiles', `${strategy}.json`);
        const data = await readJsonFile(file, { profiles: [] });
        if (data.profiles.some(item => item.name === name)) throw new LabError(`profile ${name} already exists`, 409);
        data.profiles.push({ name, parameters: clean, source_job, created_at: this.now().toISOString() });
        await writeJsonAtomic(file, data);
        return data.profiles;
    }

    async livePresent(strategy) {
        const settings = await loadOptionsSettings(this.root);
        const allowed = tunableKeys(await this.baseConfig(), strategy);
        const section = settings[strategy] || {};
        return Object.fromEntries(allowed.filter(key => key in section).map(key => [key, section[key]]));
    }

    async promotePreview(strategy, profileName) {
        if (!OPTIONS_STRATEGIES.includes(strategy)) throw new LabError('strategy must be vrp or long_volatility');
        const parameters = await this.profileParameters(strategy, profileName);
        const current = await this.livePresent(strategy);
        const diff = diffParameters(current, parameters);
        return { strategy, profile: profileName, diff, changes: diff.filter(item => item.changed).length };
    }

    async promote(strategy, profileName) {
        const preview = await this.promotePreview(strategy, profileName);
        const parameters = await this.profileParameters(strategy, profileName);
        const overridePath = optionsConfigPath(this.root);
        const override = await readJsonFile(overridePath, {});
        const section = { ...(override[strategy] || {}) };
        const previous = Object.fromEntries(Object.keys(parameters).map(key => [key, key in section
            ? { existed: true, value: section[key] }
            : { existed: false }]));
        const next = { ...override, [strategy]: { ...section, ...parameters } };
        const defaults = await readJson(path.join(this.root, 'options', 'config.default.json'));
        const merged = mergeConfig(defaults, next);
        validateConfigShape(merged, defaults);
        validateOptionsConfig(merged);
        await writeJsonAtomic(overridePath, next);
        const entry = { id: randomUUID().slice(0, 8), action: 'promote', strategy, profile: profileName, previous, applied: parameters, at: this.now().toISOString() };
        await this.appendHistory(strategy, entry);
        return { ...preview, applied: 'next options paper cycle', entry };
    }

    async history(strategy) {
        return (await readJsonFile(path.join(this.labDir, 'history', `${strategy}.json`), { entries: [] })).entries;
    }

    async appendHistory(strategy, entry) {
        const file = path.join(this.labDir, 'history', `${strategy}.json`);
        const data = await readJsonFile(file, { entries: [] });
        data.entries.push(entry);
        await writeJsonAtomic(file, data);
    }

    async rollback(strategy) {
        const entries = await this.history(strategy);
        const target = [...entries].reverse().find(item => item.action === 'promote' && !item.rolled_back_at);
        if (!target) throw new LabError(`nothing to roll back for ${strategy}`, 409);
        const overridePath = optionsConfigPath(this.root);
        const override = await readJsonFile(overridePath, {});
        const section = { ...(override[strategy] || {}) };
        for (const [key, info] of Object.entries(target.previous)) {
            if (info.existed) section[key] = info.value;
            else delete section[key];
        }
        const next = { ...override, [strategy]: section };
        const defaults = await readJson(path.join(this.root, 'options', 'config.default.json'));
        const merged = mergeConfig(defaults, next);
        validateConfigShape(merged, defaults);
        validateOptionsConfig(merged);
        await writeJsonAtomic(overridePath, next);
        const at = this.now().toISOString();
        const data = await readJsonFile(path.join(this.labDir, 'history', `${strategy}.json`), { entries: [] });
        data.entries = data.entries.map(item => (item.id === target.id ? { ...item, rolled_back_at: at } : item));
        data.entries.push({ id: randomUUID().slice(0, 8), action: 'rollback', strategy, restored_entry: target.id, at });
        await writeJsonAtomic(path.join(this.labDir, 'history', `${strategy}.json`), data);
        return { strategy, restored: target.id, applied: 'next options paper cycle' };
    }

    async overview() {
        const options = await Promise.all(OPTIONS_STRATEGIES.map(async strategy => ({
            id: strategy,
            account: 'options',
            parameters: await this.livePresent(strategy),
            tunable: tunableKeys(await this.baseConfig(), strategy),
            profiles: await this.listProfiles(strategy),
            history: (await this.history(strategy)).slice(-10),
        })));
        return {
            limits: { maxUnderlyings: LIMITS.maxUnderlyings, minFreeMemoryMb: LIMITS.minFreeMemoryMb, earliestStart: LIMITS.earliestStart, concurrentJobs: 1 },
            active_job: this.activeJob(),
            options,
            equity: EQUITY_STRATEGIES.map(id => ({ id, account: 'equity', start_available: false })),
        };
    }
}

module.exports = {
    LIMITS,
    LabError,
    ResearchLab,
    buildJobConfig,
    diffParameters,
    parseFreeMemoryMb,
    parseLabEvents,
    summarizeProgress,
    tunableKeys,
    validateJobRequest,
    validateParameters,
};
