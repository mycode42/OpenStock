const fs = require('node:fs');
const fsp = require('node:fs/promises');
const path = require('node:path');
const { randomUUID } = require('node:crypto');
const { spawn, spawnSync } = require('node:child_process');
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
const EQUITY_STRATEGIES = Object.freeze([
    'swing_trend_following', 'swing_momentum', 'swing_mean_reversion', 'swing_multi_factor',
    'intraday_mean_reversion', 'intraday_trend_following', 'intraday_momentum',
]);
const EVENT_PREFIX = 'LAB_EVENT ';
const LOG_CHUNK_BYTES = 64 * 1024;
const TICKER_PATTERN = /^[A-Z][A-Z0-9.]{0,9}$/;
const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;
const PROFILE_NAME_PATTERN = /^[a-z0-9][a-z0-9_-]{0,39}$/i;

// Built-in equity profiles are multipliers on the live values. Standard is the live values as they are.
// The factors move the risk-related parameters only; the frozen gates and the portfolio caps are not parameters and are not changed here.
const EQUITY_PRESET_NAMES = Object.freeze(['standard', 'conservative', 'high-risk']);
const EQUITY_PRESET_FACTORS = Object.freeze({
    conservative: {
        max_position_pct: 0.7, risk_multiplier: 0.6, hard_stop_pct: 0.8, trailing_stop_pct: 0.8,
        trailing_activation_pct: 0.8, max_entries_per_day: 0.66, max_positions: 0.7, cooldown_minutes: 1.5,
    },
    'high-risk': {
        max_position_pct: 1.3, risk_multiplier: 1.4, hard_stop_pct: 1.2, trailing_stop_pct: 1.2,
        trailing_activation_pct: 1.2, max_entries_per_day: 1.5, max_positions: 1.3, cooldown_minutes: 0.5,
    },
});
const PRESET_COUNT_KEYS = new Set(['max_entries_per_day', 'max_positions', 'cooldown_minutes']);
// Keys each equity strategy's validator accepts for a profile (kept in step with strategies/registry.py by a test).
const EQUITY_PROFILE_KEYS = require('./equity-profile-keys.json');
function equityProfileKeys(strategy) {
    return Array.isArray(EQUITY_PROFILE_KEYS[strategy]) ? EQUITY_PROFILE_KEYS[strategy] : [];
}

// Built-in options profiles: multipliers on the live grid values. Conservative needs a higher IV edge and smaller deltas and risk.
const OPTIONS_PRESET_FACTORS = Object.freeze({
    vrp: {
        conservative: { min_iv_minus_rv: 1.5, short_delta_min: 0.8, wing_delta_max: 0.8 },
        'high-risk': { min_iv_minus_rv: 0.75, short_delta_min: 1.2, wing_delta_max: 1.2 },
    },
    long_volatility: {
        conservative: { max_risk_per_trade_pct: 0.5, max_premium_per_trade_usd: 0.6, stop_loss_pct: 0.8, iv_percentile_ceiling: 0.9 },
        'high-risk': { max_risk_per_trade_pct: 1.5, max_premium_per_trade_usd: 1.5, stop_loss_pct: 1.2, iv_percentile_ceiling: 1.1 },
    },
});
const RESERVED_PROFILE_NAMES = Object.freeze(['standard', 'conservative', 'high-risk']);

function optionsPresetParameters(strategy, name, livePresent) {
    if (name === 'standard') return { ...livePresent };
    const factors = OPTIONS_PRESET_FACTORS[strategy]?.[name];
    if (!factors) throw new LabError(`unknown profile ${name}`, 404);
    const out = {};
    for (const [key, factor] of Object.entries(factors)) {
        if (typeof livePresent[key] === 'number') out[key] = Number((livePresent[key] * factor).toFixed(4));
    }
    return out;
}

function equityPresetParameters(name, livePresent, strategy) {
    const live = scalarParameters(livePresent);
    const allowed = equityProfileKeys(strategy);
    if (name === 'standard') return Object.fromEntries(Object.entries(live).filter(([key, value]) => allowed.includes(key) && typeof value === 'number'));
    const factors = EQUITY_PRESET_FACTORS[name];
    if (!factors) throw new LabError(`unknown profile ${name}`, 404);
    const out = {};
    for (const [key, factor] of Object.entries(factors)) {
        if (!allowed.includes(key) || typeof live[key] !== 'number') continue;
        const scaled = live[key] * factor;
        out[key] = PRESET_COUNT_KEYS.has(key) ? Math.max(key === 'cooldown_minutes' ? 0 : 1, Math.round(scaled)) : Number(scaled.toFixed(4));
    }
    return out;
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

// Equity runs research the chosen strategies, or every researchable strategy when none are chosen (a full run).
function validateEquityJobRequest(body) {
    const requested = body.strategies === undefined || body.strategies === null ? [] : body.strategies;
    // An empty list is a full run only when the request asks for one. Otherwise it is an unfinished choice, and it is refused.
    if (Array.isArray(requested) && requested.length === 0 && body.full_run !== true) {
        throw new LabError('choose at least one strategy, or ask for the full run');
    }
    if (!Array.isArray(requested) || requested.some(item => typeof item !== 'string')) {
        throw new LabError('strategies must be a list of strategy names');
    }
    const strategies = [...new Set(requested.map(item => item.trim()).filter(Boolean))];
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
        strategies: strategies.length ? strategies : [...EQUITY_STRATEGIES],
        profiles,
        full_run: strategies.length === 0,
        no_refresh: body.no_refresh === true,
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
        if (request.no_refresh) args.push('--no-refresh');
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
            id, account: 'equity', strategy: request.full_run ? 'full run' : request.strategies.join(', '),
            strategies: request.strategies, full_run: request.full_run, no_refresh: request.no_refresh, profiles: request.profiles,
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

    // The equity run's report is in its own output folder. Each strategy's artifact status is listed when the report has one.
    async equityJobResults(job) {
        const report = await readJsonFile(path.join(this.jobDir(job.id), 'output', 'latest.json'), null);
        const artifacts = report?.adaptive_strategy_artifacts && typeof report.adaptive_strategy_artifacts === 'object'
            ? report.adaptive_strategy_artifacts : {};
        const strategies = Object.entries(artifacts).map(([name, artifact]) => ({
            strategy: name, status: artifact?.status ?? null,
        }));
        return {
            job: job.id, strategy: job.strategy, state: job.state, account: 'equity',
            report: report ? { status: report.status ?? null, created_at: report.created_at ?? null, portfolio_summary: report.portfolio_summary ?? null } : null,
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

    async getPlan(account) {
        return readJsonFile(this.planPath(account), { account, profiles: {} });
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
        const live = await this.livePresent(strategy);
        return RESERVED_PROFILE_NAMES.map(name => ({ name, builtin: true, parameters: optionsPresetParameters(strategy, name, live) }));
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
    async equityPlanPreview() {
        const plan = await this.getPlan('equity');
        const settings = await loadSettings(this.root);
        const strategies = [];
        for (const id of EQUITY_STRATEGIES) {
            const name = plan.profiles?.[id];
            if (!name || name === 'standard') continue;
            const live = scalarParameters(settings.strategies?.[id]);
            const profile = (await this.listEquityProfiles(id, live)).find(item => item.name === name);
            if (!profile) throw new LabError(`profile ${name} was not found for ${id}`, 404);
            strategies.push({ strategy: id, profile: name, parameters: profile.parameters, diff: diffParameters(live, profile.parameters) });
        }
        return { account: 'equity', strategies, changes: strategies.reduce((total, item) => total + item.diff.filter(row => row.changed).length, 0) };
    }

    async equityPlanApply() {
        const preview = await this.equityPlanPreview();
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
            previous_override: previousOverride, at: this.now().toISOString(),
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
            return {
                ...base,
                start_available: false,
                strategies: await Promise.all(EQUITY_STRATEGIES.map(async id => ({
                    id, account: 'equity', parameters: scalarParameters(settings.strategies?.[id]),
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
        return { ...base, start_available: true, strategies };
    }
}

module.exports = {
    equityPresetParameters, EQUITY_PRESET_NAMES,
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
