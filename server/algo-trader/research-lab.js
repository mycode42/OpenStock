const fs = require('node:fs');
const fsp = require('node:fs/promises');
const path = require('node:path');
const { randomUUID } = require('node:crypto');
const { execFile, spawn, spawnSync } = require('node:child_process');
const { promisify } = require('node:util');
const execFileAsync = promisify(execFile);
const { readJson } = require('./io');
const { loadOptionsSettings, loadSettings, mergeConfig, optionsConfigPath, validateConfigShape, validateOptionsConfig } = require('./settings');
const { loadRules } = require('./settings-rules');
const { validateSettings, writeSettings } = require('./settings');

const LIMITS = Object.freeze({
    maxUnderlyings: 20,
    minFreeMemoryMb: 6144,
    earliestStart: '2020-01-01',
});
const OPTIONS_STRATEGIES = Object.freeze(['vrp', 'long_volatility']);
// Equity pipeline stages in the order the research runs them, as the stage file names them.
const EQUITY_STAGES = Object.freeze([
    ['data_refresh', 'Data refresh'], ['factor_model', 'Factor model'], ['discovery', 'Discovery search'],
    ['verification', 'Rolling verification'], ['candidate_validation', 'Candidate validation'],
    ['portfolio_replay', 'Portfolio replay'], ['forward_validation', 'Forward validation'], ['report_assembly', 'Report'],
]);
const EQUITY_STRATEGIES = Object.freeze([
    'swing_trend_following', 'swing_momentum', 'swing_mean_reversion', 'swing_multi_factor',
    'intraday_mean_reversion', 'intraday_trend_following', 'intraday_momentum',
]);
const EVENT_PREFIX = 'LAB_EVENT ';
const LOG_CHUNK_BYTES = 64 * 1024;
const TICKER_PATTERN = /^[A-Z][A-Z0-9.]{0,9}$/;
const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;
const PROFILE_NAME_PATTERN = /^[a-z0-9][a-z0-9_-]{0,39}$/i;

// The built-in profiles are fixed values in profile-presets.json, not calculated. Standard is the live values on 2026-11-08.
// Applying a profile never changes these numbers, so applying twice gives the same result.
const PROFILE_PRESETS = require('./profile-presets.json');
const EQUITY_PRESET_NAMES = Object.freeze(['standard', 'conservative', 'high-risk']);
const RESERVED_PROFILE_NAMES = Object.freeze(['standard', 'conservative', 'high-risk']);
// Keys each equity strategy's validator accepts for a profile (kept in step with strategies/registry.py by a test).
const EQUITY_PROFILE_KEYS = require('./equity-profile-keys.json');
function equityProfileKeys(strategy) {
    return Array.isArray(EQUITY_PROFILE_KEYS[strategy]) ? EQUITY_PROFILE_KEYS[strategy] : [];
}

function fixedPreset(group, strategy, name) {
    const values = PROFILE_PRESETS[group]?.[strategy]?.[name];
    if (!values) throw new LabError(`unknown profile ${name}`, 404);
    return { ...values };
}

function optionsPresetParameters(strategy, name) {
    return fixedPreset('options', strategy, name);
}

function equityPresetParameters(name, livePresent, strategy) {
    return fixedPreset('equity', strategy, name);
}

// Keeps the values a panel can show as plain text: numbers, booleans, strings, and short lists of strings.
function scalarParameters(section) {
    const out = {};
    for (const [key, value] of Object.entries(section && typeof section === 'object' ? section : {})) {
        const list = Array.isArray(value) && value.every(item => typeof item === 'string');
        if (typeof value === 'number' || typeof value === 'boolean' || typeof value === 'string' || list) out[key] = value;
    }
    return out;
}

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
    if (body.account === 'equity') return validateEquityJobRequest(body);
    if (body.account !== 'options') throw new LabError('account must be equity or options', 400);
    // One or more options strategies. Each runs as its own job, so the rest wait in a queue.
    const optionStrategies = Array.isArray(body.strategies) ? [...new Set(body.strategies)] : [body.strategy];
    if (!optionStrategies.length || optionStrategies.some(item => !OPTIONS_STRATEGIES.includes(item))) {
        throw new LabError(`strategy must be one of ${OPTIONS_STRATEGIES.join(', ')}`);
    }
    if (optionStrategies.length > 2) throw new LabError('at most 2 options strategies per run');
    const optionProfiles = isPlainObject(body.profiles) ? body.profiles : {};
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
    const first = optionStrategies[0];
    return {
        account: 'options', strategies: optionStrategies, strategy: first, underlyings, start, end,
        profile: optionProfiles[first] && optionProfiles[first] !== 'standard' ? optionProfiles[first] : (body.profile || null),
        profiles: optionProfiles,
    };
}

// Equity runs research one or more chosen strategies, always on the latest stored data.
function validateEquityJobRequest(body) {
    const requested = body.strategies === undefined || body.strategies === null ? [] : body.strategies;
    if (!Array.isArray(requested) || requested.some(item => typeof item !== 'string')) {
        throw new LabError('strategies must be a list of strategy names');
    }
    if (requested.length === 0) throw new LabError('choose at least one strategy');
    const strategies = [...new Set(requested.map(item => item.trim()).filter(Boolean))];
    if (!strategies.length) throw new LabError('choose at least one strategy');
    const unknown = strategies.filter(item => !EQUITY_STRATEGIES.includes(item));
    if (unknown.length) throw new LabError(`unknown equity strategies: ${unknown.join(', ')}`);
    const tickers = Array.isArray(body.underlyings) ? [...new Set(body.underlyings.map(item => String(item).trim().toUpperCase()).filter(Boolean))] : [];
    if (tickers.some(item => !TICKER_PATTERN.test(item))) throw new LabError('every underlying must be a ticker symbol');
    if (tickers.length > LIMITS.maxUnderlyings) throw new LabError(`at most ${LIMITS.maxUnderlyings} underlyings per run`);
    const start = body.start ? String(body.start) : null;
    const end = body.end ? String(body.end) : null;
    if (start && (!isIsoDate(start) || start < LIMITS.earliestStart)) throw new LabError(`start must be a date on or after ${LIMITS.earliestStart}`);
    if (end && !isIsoDate(end)) throw new LabError('end must be a date');
    if (start && end && start >= end) throw new LabError('start must be before end');
    const profiles = body.profiles === undefined || body.profiles === null ? {} : body.profiles;
    if (!isPlainObject(profiles)) throw new LabError('profiles must be an object of strategy to profile name');
    for (const [strategy, name] of Object.entries(profiles)) {
        if (!EQUITY_STRATEGIES.includes(strategy)) throw new LabError(`${strategy} is not an equity strategy`);
        if (typeof name !== 'string' || !PROFILE_NAME_PATTERN.test(name)) throw new LabError('profile names must be simple names');
    }
    return {
        account: 'equity',
        strategy: null,
        strategies,
        profiles,
        underlyings: tickers, start, end, profile: null,
    };
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
    constructor({ root, python = process.env.ALGO_TRADER_PYTHON, now = () => new Date(), spawnImpl = spawn, meminfoPath = '/proc/meminfo', nightlyRunning = null, checkProfiles = null }) {
        this.nightlyRunning = nightlyRunning;
        this.queue = [];
        this.checkProfiles = checkProfiles;
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
        if (request.account === 'equity') return this.startEquityJob(request);
        // Queued strategies run one after another, each with its own profile, once the job before has finished.
        const queued = request.strategies.slice(1).map(strategy => ({
            account: 'options', strategies: [strategy], underlyings: request.underlyings,
            start: request.start, end: request.end, profiles: request.profiles,
        }));
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
        const executable = configured || 'poetry';
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
        this.queue = queued;
        child.on('error', error => this.finishJob(id, { state: 'failed', error: error.message }));
        child.on('exit', (code, signal) => {
            const state = job.cancel_requested ? 'cancelled' : (code === 0 ? 'finished' : 'failed');
            this.finishJob(id, { state, exit_code: code, signal: signal || null });
        });
        return job;
    }

    // The strategy validator decides which profile values are in range. A value outside it is refused before a run or an apply.
    async checkProfileRanges(overrides) {
        if (!Object.keys(overrides).length) return;
        if (this.checkProfiles) return this.checkProfiles(overrides);
        const script = path.join(this.root, 'scripts', 'lab_check_profile.py');
        const executable = this.python || 'poetry';
        const command = this.python ? [script] : ['run', 'python', script];
        const result = spawnSync(executable, command, { cwd: this.root, input: JSON.stringify(overrides), encoding: 'utf8', timeout: 60_000 });
        let errors = null;
        try { errors = JSON.parse(result.stdout || '').errors; } catch { errors = null; }
        if (!errors) throw new LabError('could not check the profile values against the strategy ranges', 503);
        const messages = Object.entries(errors).map(([strategy, message]) => `${strategy}: ${message}`);
        if (messages.length) throw new LabError(`profile values are outside the strategy ranges. ${messages.join('; ')}`, 422);
    }

    // A lab equity run writes only into its own job folder. It waits for the nightly equity run, so the two never share the machine.
    async startEquityJob(request) {
        if (await this.nightlyEquityRunning()) {
            throw new LabError('Nightly equity research is running now. Start a lab run after it finishes, or stop it with Kill running research.', 409);
        }
        const freeMb = await this.freeMemoryMb();
        if (freeMb < LIMITS.minFreeMemoryMb) {
            throw new LabError(`only ${freeMb} MB of memory is free; a run needs ${LIMITS.minFreeMemoryMb} MB`, 503);
        }
        // Each strategy's chosen profile becomes a parameter file for this run only. Standard is the live values, so it needs no file.
        const settings = await loadSettings(this.root);
        const overrides = {};
        for (const strategy of request.strategies) {
            const name = request.profiles[strategy] || 'standard';
            if (name === 'standard') continue;
            const live = scalarParameters(settings.strategies?.[strategy]);
            const profile = (await this.listEquityProfiles(strategy, live)).find(item => item.name === name);
            if (!profile) throw new LabError(`profile ${name} was not found for ${strategy}`, 404);
            overrides[strategy] = profile.parameters;
        }
        await this.checkProfileRanges(overrides);
        const id = `${this.now().toISOString().replace(/[:.]/g, '-')}-${randomUUID().slice(0, 8)}`;
        const dir = this.jobDir(id);
        const outputDir = path.join(dir, 'output');
        await fsp.mkdir(outputDir, { recursive: true, mode: 0o700 });
        const overridesPath = path.join(dir, 'overrides.json');
        if (Object.keys(overrides).length) await writeJsonAtomic(overridesPath, overrides);
        const logPath = path.join(dir, 'log.txt');
        const logFd = fs.openSync(logPath, 'a');
        const args = ['-m', 'equity_research.auto_research', '--lab-run', '--output-dir', outputDir,
            '--workers', '8', '--strategies', request.strategies.join(',')];
        if (request.underlyings.length) args.push('--underlyings', request.underlyings.join(','));
        if (request.start) args.push('--start', request.start);
        if (request.end) args.push('--end', request.end);
        const configured = this.python;
        const executable = configured || 'poetry';
        const command = configured ? args : ['run', 'python', ...args];
        const env = { ...process.env };
        if (Object.keys(overrides).length) env.ALGO_LAB_PARAMETER_OVERRIDES = overridesPath;
        const child = this.spawnImpl(executable, command, {
            cwd: this.root,
            env,
            detached: true,
            stdio: ['ignore', logFd, logFd],
        });
        fs.closeSync(logFd);
        const job = {
            id, account: 'equity', strategy: request.strategies.join(', '),
            strategies: request.strategies, profiles: request.profiles,
            underlyings: request.underlyings, start: request.start, end: request.end, profile: null,
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

    async nightlyEquityRunning() {
        return this.nightlyRunning ? this.nightlyRunning() : false;
    }

    async finishJob(id, patch) {
        this.children.delete(id);
        let job = null;
        try {
            job = await readJsonFile(path.join(this.jobDir(id), 'job.json'), null);
            if (!job) return;
            await writeJsonAtomic(path.join(this.jobDir(id), 'job.json'), { ...job, ...patch, pid: null, updated_at: this.now().toISOString() });
        } catch (error) {
            console.error(`Research lab could not record job ${id}: ${error.message}`);
        }
        if (job?.account === 'options' && this.queue.length && !this.activeJob()) {
            const next = this.queue.shift();
            try {
                await this.startJob(next);
            } catch (error) {
                this.queue = [];
                console.error(`Research lab could not start the next queued run: ${error.message}`);
            }
        }
    }

    async cancelJob(id) {
        this.queue = [];
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
        const stages = job.account === 'equity' ? await this.equityStages(job) : null;
        return { ...job, progress: summarizeProgress(events), stages, log_bytes: log.size };
    }

    // One row per pipeline stage, from the run's stage file. A stage that never ran reads "not run" once the run has finished.
    async equityStages(job) {
        const progress = await readJsonFile(path.join(this.jobDir(job.id), 'output', 'latest-progress.json'), null) || {};
        const stages = progress.stages || {};
        const perStrategy = progress.strategies || {};
        const total = (job.strategies || []).length;
        const finished = job.state !== 'running' && job.state !== 'cancelling';
        const settled = new Set(['completed', 'reused', 'skipped', 'error', 'failed', 'not_run']);
        const rows = EQUITY_STAGES.map(([key, label]) => {
            let status = stages[key]?.status || 'pending';
            if (status === 'pending' && finished) status = 'not_run';
            const strategyStatuses = perStrategy[key] ? Object.values(perStrategy[key]).map(item => item.status) : [];
            return {
                key, label, status, updated_at: stages[key]?.updated_at ?? null,
                strategies: strategyStatuses.length ? { done: strategyStatuses.filter(value => settled.has(value)).length, total } : null,
            };
        });
        const settledCount = rows.filter(row => settled.has(row.status)).length;
        const running = rows.filter(row => row.status === 'running').length;
        const percent = job.state === 'finished' ? 100 : Math.round(((settledCount + 0.5 * running) / rows.length) * 100);
        return { rows, percent, current_stage: progress.current_stage ?? null, updated_at: progress.updated_at ?? null };
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
        if (job.account === 'equity') return this.equityJobResults(job);
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

    // In-sample numbers from the discovery search: the run's starting parameters and the best candidate it found.
    // Only artifacts inside this job's folder are read.
    async equityDiscoveryMetrics(job, artifact) {
        const file = artifact?.discovery;
        if (!file || !String(file).startsWith(this.jobDir(job.id) + path.sep)) return null;
        const detail = await readJsonFile(file, null);
        if (!detail) return null;
        const pick = candidate => {
            if (!candidate) return null;
            const metrics = candidate.metrics || {};
            return {
                eligible: candidate.eligible ?? null,
                parameters: candidate.parameters ?? null,
                net_return_pct: metrics.net_return_pct ?? null,
                final_equity: metrics.final_equity ?? null,
                closed_trades: metrics.closed_trade_count ?? null,
                max_drawdown_pct: metrics.max_drawdown_pct ?? null,
                profit_factor: metrics.profit_factor ?? null,
                win_rate_pct: metrics.win_rate_pct ?? null,
            };
        };
        return {
            window: detail.window ?? null,
            selection_status: detail.selection_status ?? null,
            min_trades: detail.min_trades_for_selection ?? null,
            start: pick(detail.baseline_comparison?.baseline_candidate),
            best: pick(detail.selected_candidate),
        };
    }

    // The equity run's report is in its own output folder. Each strategy's artifact status is listed when the report has one.
    async equityJobResults(job) {
        const report = await readJsonFile(path.join(this.jobDir(job.id), 'output', 'latest.json'), null);
        const artifacts = report?.adaptive_strategy_artifacts && typeof report.adaptive_strategy_artifacts === 'object'
            ? report.adaptive_strategy_artifacts : {};
        const strategies = await Promise.all(Object.entries(artifacts).map(async ([name, artifact]) => ({
            strategy: name, status: artifact?.status ?? null,
            metrics: await this.equityDiscoveryMetrics(job, artifact),
        })));
        return {
            job: job.id, strategy: job.strategy, state: job.state, account: 'equity',
            report: report ? {
                status: report.status ?? null, created_at: report.created_at ?? null, portfolio_summary: report.portfolio_summary ?? null,
                discovery_only_reason: report.discovery_only_reason ?? null,
            } : null,
            strategies,
        };
    }

    // Built-in profiles come first, then the profiles saved in the lab.
    async listProfiles(strategy) {
        const file = path.join(this.labDir, 'profiles', `${strategy}.json`);
        const data = await readJsonFile(file, { profiles: [] });
        const builtin = OPTIONS_STRATEGIES.includes(strategy)
            ? await this.builtinProfiles(strategy)
            : [];
        return [...builtin, ...data.profiles];
    }

    // Equity profiles: the three built-ins from the live values, then custom profiles saved for that strategy.
    // A custom equity profile sets the tunable risk settings of one strategy. Values must be numbers within the built-in ranges' sanity bounds.
    async saveEquityProfile({ strategy, name, parameters, source_job = null }) {
        if (!EQUITY_STRATEGIES.includes(strategy)) throw new LabError('strategy must be an equity strategy');
        if (!PROFILE_NAME_PATTERN.test(name || '')) throw new LabError('profile name must be letters, digits, dashes or underscores');
        if (RESERVED_PROFILE_NAMES.includes(name)) throw new LabError(`${name} is a built-in profile name`, 409);
        if (!isPlainObject(parameters)) throw new LabError('parameters must be an object');
        const allowed = equityProfileKeys(strategy);
        if (!allowed.length) throw new LabError(`${strategy} takes no profile settings`, 409);
        const clean = {};
        for (const [key, value] of Object.entries(parameters)) {
            if (!allowed.includes(key)) throw new LabError(`${key} is not a tunable equity parameter`);
            if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) throw new LabError(`${key} must be a non-negative number`);
            clean[key] = value;
        }
        if (!Object.keys(clean).length) throw new LabError('set at least one parameter');
        const file = path.join(this.labDir, 'profiles', `equity-${strategy}.json`);
        const data = await readJsonFile(file, { profiles: [] });
        if (data.profiles.some(item => item.name === name)) throw new LabError(`profile ${name} already exists`, 409);
        data.profiles.push({ name, parameters: clean, source_job, created_at: this.now().toISOString() });
        await writeJsonAtomic(file, data);
        return data.profiles;
    }

    async listEquityProfiles(strategy, livePresent) {
        const data = await readJsonFile(path.join(this.labDir, 'profiles', `equity-${strategy}.json`), { profiles: [] });
        // A strategy whose validator accepts no profile settings gets no profiles.
        if (!equityProfileKeys(strategy).length) return [];
        const builtin = EQUITY_PRESET_NAMES.map(name => ({ name, builtin: true, parameters: equityPresetParameters(name, livePresent, strategy) }));
        return [...builtin, ...data.profiles];
    }

    // The plan is the profile chosen for each strategy in an account, kept between visits.
    planPath(account) {
        return path.join(this.labDir, `plan-${account}.json`);
    }

    planHistoryPath(account) {
        return path.join(this.labDir, 'history', account === 'equity' ? 'equity-plan.json' : 'options-plan.json');
    }

    // The saved plan, plus the last apply and the recent applies, so the page can show what is live.
    async getPlan(account) {
        const plan = await readJsonFile(this.planPath(account), { account, profiles: {} });
        const { entries } = await readJsonFile(this.planHistoryPath(account), { entries: [] });
        const summarize = entry => ({
            id: entry.id, at: entry.at, changes: entry.changes ?? null,
            profiles: (entry.applied || []).map(item => `${item.strategy}: ${item.profile}`),
            rolled_back_at: entry.rolled_back_at || null,
        });
        const applied = entries.filter(entry => entry.action === 'apply_plan');
        const live = applied.slice().reverse().find(entry => !entry.rolled_back_at);
        return { ...plan, last_applied: live ? summarize(live) : null, history: applied.slice(-5).reverse().map(summarize) };
    }

    async setPlan(account, profiles) {
        if (account !== 'equity' && account !== 'options') throw new LabError('account must be equity or options');
        if (!isPlainObject(profiles)) throw new LabError('profiles must be an object of strategy to profile name');
        const strategies = account === 'equity' ? EQUITY_STRATEGIES : OPTIONS_STRATEGIES;
        const clean = {};
        for (const [strategy, name] of Object.entries(profiles)) {
            if (!strategies.includes(strategy)) throw new LabError(`${strategy} is not a ${account} strategy`);
            if (typeof name !== 'string' || !PROFILE_NAME_PATTERN.test(name)) throw new LabError('profile names must be simple names');
            const known = account === 'equity'
                ? await this.listEquityProfiles(strategy, scalarParameters((await loadSettings(this.root)).strategies?.[strategy]))
                : await this.listProfiles(strategy);
            if (!known.some(item => item.name === name)) throw new LabError(`profile ${name} was not found for ${strategy}`, 404);
            clean[strategy] = name;
        }
        const plan = { account, profiles: clean, updated_at: this.now().toISOString() };
        await writeJsonAtomic(this.planPath(account), plan);
        return plan;
    }

    async builtinProfiles(strategy) {
        return RESERVED_PROFILE_NAMES.map(name => ({ name, builtin: true, parameters: optionsPresetParameters(strategy, name) }));
    }

    async profileParameters(strategy, name) {
        const profile = (await this.listProfiles(strategy)).find(item => item.name === name);
        if (!profile) throw new LabError(`profile ${name} was not found`, 404);
        return profile.parameters;
    }

    async saveProfile({ account = 'options', strategy, name, parameters, source_job = null }) {
        if (account === 'equity') return this.saveEquityProfile({ strategy, name, parameters, source_job });
        if (!OPTIONS_STRATEGIES.includes(strategy)) throw new LabError('strategy must be vrp or long_volatility');
        if (!PROFILE_NAME_PATTERN.test(name || '')) throw new LabError('profile name must be letters, digits, dashes or underscores');
        if (RESERVED_PROFILE_NAMES.includes(name)) throw new LabError(`${name} is a built-in profile name`, 409);
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

    // Equity plan: the chosen profiles, applied to the live equity settings as one change with a diff and a rollback.
    // Live values the settings file does not hold, read from the registry the runtime uses. Cached briefly.
    async registryLiveParameters(keysByStrategy) {
        const request = JSON.stringify(keysByStrategy);
        if (this.registryLive?.request === request && Date.now() - this.registryLive.at < 60_000) return this.registryLive.values;
        const script = path.join(this.root, 'scripts', 'lab_live_parameters.py');
        const executable = this.python || 'poetry';
        const command = this.python ? [script, request] : ['run', 'python', script, request];
        let values = {};
        try {
            const { stdout } = await execFileAsync(executable, command, { cwd: this.root, timeout: 60_000, encoding: 'utf8' });
            values = JSON.parse(stdout || '{}');
        } catch {
            values = {};
        }
        this.registryLive = { request, at: Date.now(), values };
        return values;
    }

    async equityPlanPreview() {
        const plan = await this.getPlan('equity');
        const settings = await loadSettings(this.root);
        const strategies = [];
        const notApplied = [];
        for (const id of EQUITY_STRATEGIES) {
            const name = plan.profiles?.[id];
            if (!name || name === 'standard') continue;
            // Intraday profiles are tested in lab runs. Applying them needs the promotion path, so they are listed here, not written.
            if (id.startsWith('intraday_')) {
                notApplied.push({ strategy: id, profile: name });
                continue;
            }
            const live = scalarParameters(settings.strategies?.[id]);
            const profile = (await this.listEquityProfiles(id, live)).find(item => item.name === name);
            if (!profile) throw new LabError(`profile ${name} was not found for ${id}`, 404);
            strategies.push({ strategy: id, profile: name, parameters: profile.parameters, diff: diffParameters(live, profile.parameters) });
        }
        return {
            account: 'equity', strategies, not_applied: notApplied,
            changes: strategies.reduce((total, item) => total + item.diff.filter(row => row.changed).length, 0),
        };
    }

    async equityPlanApply() {
        const preview = await this.equityPlanPreview();
        if (preview.not_applied.length) {
            const names = preview.not_applied.map(item => `${item.strategy} (${item.profile})`).join(', ');
            throw new LabError(`intraday profiles can be tested in lab runs but not applied yet: ${names}. Set them to standard to apply the rest.`, 409);
        }
        if (!preview.changes) throw new LabError('the plan changes nothing from the live equity values', 409);
        await this.checkProfileRanges(Object.fromEntries(preview.strategies.map(item => [item.strategy, item.parameters])));
        const settings = await loadSettings(this.root);
        const defaults = await readJson(path.join(this.root, 'portfolio', 'algo-strategy-config.default.json'));
        const overridePath = path.join(this.root, 'data', 'algo-strategy-config.json');
        const previousOverride = await readJsonFile(overridePath, null);
        const next = structuredClone(settings);
        for (const item of preview.strategies) next.strategies[item.strategy] = { ...next.strategies[item.strategy], ...item.parameters };
        // Refuses the whole change if any value breaks the settings rules, so nothing half-applies.
        validateSettings(next, defaults, await loadRules(this.root));
        await writeSettings(this.root, next);
        const entry = {
            id: randomUUID().slice(0, 8), action: 'apply_plan', account: 'equity',
            applied: preview.strategies.map(item => ({ strategy: item.strategy, profile: item.profile, parameters: item.parameters })),
            changes: preview.changes, previous_override: previousOverride, at: this.now().toISOString(),
        };
        await this.appendEquityHistory(entry);
        return { ...preview, applied: 'next equity cycle', entry };
    }

    async equityPlanRollback() {
        const file = path.join(this.labDir, 'history', 'equity-plan.json');
        const data = await readJsonFile(file, { entries: [] });
        const target = [...data.entries].reverse().find(item => item.action === 'apply_plan' && !item.rolled_back_at);
        if (!target) throw new LabError('there is no applied equity plan to roll back', 409);
        const overridePath = path.join(this.root, 'data', 'algo-strategy-config.json');
        if (target.previous_override === null) {
            await fsp.rm(overridePath, { force: true });
        } else {
            await writeJsonAtomic(overridePath, target.previous_override);
        }
        target.rolled_back_at = this.now().toISOString();
        await writeJsonAtomic(file, data);
        return { rolled_back: target.id, restored_previous: target.previous_override !== null };
    }

    // Options plan: the chosen profile per options strategy, applied to the options override file in one change.
    async optionsPlanPreview() {
        const plan = await this.getPlan('options');
        const strategies = [];
        for (const id of OPTIONS_STRATEGIES) {
            const name = plan.profiles?.[id];
            if (!name || name === 'standard') continue;
            const live = await this.livePresent(id);
            const profile = (await this.listProfiles(id)).find(item => item.name === name);
            if (!profile) throw new LabError(`profile ${name} was not found for ${id}`, 404);
            strategies.push({ strategy: id, profile: name, parameters: profile.parameters, diff: diffParameters(live, profile.parameters) });
        }
        return { account: 'options', strategies, changes: strategies.reduce((total, item) => total + item.diff.filter(row => row.changed).length, 0) };
    }

    async optionsPlanApply() {
        const preview = await this.optionsPlanPreview();
        if (!preview.changes) throw new LabError('the plan changes nothing from the live options values', 409);
        const overridePath = optionsConfigPath(this.root);
        const previousOverride = await readJsonFile(overridePath, null);
        const override = previousOverride || {};
        const next = { ...override };
        for (const item of preview.strategies) next[item.strategy] = { ...(override[item.strategy] || {}), ...item.parameters };
        const defaults = await readJson(path.join(this.root, 'options', 'config.default.json'));
        const merged = mergeConfig(defaults, next);
        // Refuses the whole change if any value breaks the options rules, so nothing half-applies.
        validateConfigShape(merged, defaults);
        validateOptionsConfig(merged);
        await writeJsonAtomic(overridePath, next);
        const entry = {
            id: randomUUID().slice(0, 8), action: 'apply_plan', account: 'options',
            applied: preview.strategies.map(item => ({ strategy: item.strategy, profile: item.profile, parameters: item.parameters })),
            changes: preview.changes, previous_override: previousOverride, at: this.now().toISOString(),
        };
        await this.appendPlanHistory('options', entry);
        return { ...preview, applied: 'next options paper cycle', entry };
    }

    async optionsPlanRollback() {
        const file = path.join(this.labDir, 'history', 'options-plan.json');
        const data = await readJsonFile(file, { entries: [] });
        const target = [...data.entries].reverse().find(item => item.action === 'apply_plan' && !item.rolled_back_at);
        if (!target) throw new LabError('there is no applied options plan to roll back', 409);
        const overridePath = optionsConfigPath(this.root);
        if (target.previous_override === null) {
            await fsp.rm(overridePath, { force: true });
        } else {
            await writeJsonAtomic(overridePath, target.previous_override);
        }
        target.rolled_back_at = this.now().toISOString();
        await writeJsonAtomic(file, data);
        return { rolled_back: target.id, restored_previous: target.previous_override !== null };
    }

    async appendPlanHistory(account, entry) {
        if (account === 'equity') return this.appendEquityHistory(entry);
        const file = path.join(this.labDir, 'history', 'options-plan.json');
        const data = await readJsonFile(file, { entries: [] });
        data.entries.push(entry);
        await writeJsonAtomic(file, data);
    }

    // The plan routes take the account: equity or options.
    planPreview(account) {
        return account === 'options' ? this.optionsPlanPreview() : this.equityPlanPreview();
    }

    planApply(account) {
        return account === 'options' ? this.optionsPlanApply() : this.equityPlanApply();
    }

    planRollback(account) {
        return account === 'options' ? this.optionsPlanRollback() : this.equityPlanRollback();
    }

    async appendEquityHistory(entry) {
        const file = path.join(this.labDir, 'history', 'equity-plan.json');
        const data = await readJsonFile(file, { entries: [] });
        data.entries.push(entry);
        await writeJsonAtomic(file, data);
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

    async overview(account = 'options') {
        const limits = { maxUnderlyings: LIMITS.maxUnderlyings, minFreeMemoryMb: LIMITS.minFreeMemoryMb, earliestStart: LIMITS.earliestStart, concurrentJobs: 1 };
        const base = { account, limits, active_job: this.activeJob() };
        if (account === 'equity') {
            // Equity panels show the live parameters; equity runs start from the lab in a later step.
            const settings = await loadSettings(this.root);
            // Settings hold the sizing and risk values; the signal values of the intraday strategies come from the registry.
            const registryLive = await this.registryLiveParameters(Object.fromEntries(
                EQUITY_STRATEGIES.map(id => [id, equityProfileKeys(id)]).filter(([, keys]) => keys.length),
            ));
            return {
                ...base,
                start_available: false,
                strategies: await Promise.all(EQUITY_STRATEGIES.map(async id => ({
                    id, account: 'equity', parameters: { ...(registryLive[id] || {}), ...scalarParameters(settings.strategies?.[id]) },
                    tunable: equityProfileKeys(id),
                    profiles: await this.listEquityProfiles(id, scalarParameters(settings.strategies?.[id])),
                    history: [],
                }))),
            };
        }
        if (account !== 'options') throw new LabError('account must be options or equity');
        const strategies = await Promise.all(OPTIONS_STRATEGIES.map(async strategy => ({
            id: strategy,
            account: 'options',
            parameters: await this.livePresent(strategy),
            tunable: tunableKeys(await this.baseConfig(), strategy),
            profiles: await this.listProfiles(strategy),
            history: (await this.history(strategy)).slice(-10),
        })));
        return { ...base, start_available: true, strategies, queued: this.queue.map(item => item.strategies[0]) };
    }
}

module.exports = {
    equityPresetParameters, equityProfileKeys, EQUITY_PRESET_NAMES,
    EQUITY_STRATEGIES,
    LIMITS,
    LabError,
    OPTIONS_STRATEGIES,
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
