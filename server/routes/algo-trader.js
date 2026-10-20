const fs = require('node:fs/promises');
const path = require('node:path');
const { randomUUID } = require('node:crypto');

const DEFAULT_SETTINGS = Object.freeze({
    overnight_entries_enabled: true,
    enable_intraday_trend: false,
    enable_intraday_momentum: false,
    intraday_satellite_position_pct: 0.01,
    intraday_satellite_risk_multiplier: 0.25,
    algo_multi_factor_enabled: true,
    algo_event_driven_enabled: true,
});

const BOOLEAN_SETTINGS = new Set([
    'overnight_entries_enabled',
    'enable_intraday_trend',
    'enable_intraday_momentum',
    'algo_multi_factor_enabled',
    'algo_event_driven_enabled',
]);

const NUMERIC_LIMITS = Object.freeze({
    intraday_satellite_position_pct: { min: 0.001, max: 0.04 },
    intraday_satellite_risk_multiplier: { min: 0.05, max: 1 },
});
const researchSummaryCache = new Map();

function finite(value) {
    if (value === null || value === undefined || value === '') return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
}

function safeObject(value) {
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
}

async function readJson(filePath, { optional = false } = {}) {
    try {
        const value = JSON.parse(await fs.readFile(filePath, 'utf8'));
        if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Expected JSON object');
        return value;
    } catch (error) {
        if (optional && error.code === 'ENOENT') return null;
        throw error;
    }
}

function validateSettings(value) {
    const settings = { ...DEFAULT_SETTINGS, ...safeObject(value) };
    const unknown = Object.keys(safeObject(value)).filter(key => !Object.hasOwn(DEFAULT_SETTINGS, key));
    if (unknown.length) throw new Error(`Unsupported setting: ${unknown[0]}`);
    for (const key of BOOLEAN_SETTINGS) {
        if (typeof settings[key] !== 'boolean') throw new Error(`${key} must be boolean`);
    }
    for (const [key, { min, max }] of Object.entries(NUMERIC_LIMITS)) {
        const number = finite(settings[key]);
        if (number === null || number < min || number > max) {
            throw new Error(`${key} must be between ${min} and ${max}`);
        }
        settings[key] = number;
    }
    return settings;
}

async function loadSettings(dataDir) {
    const overrides = await readJson(path.join(dataDir, 'dashboard-runtime-overrides.json'), { optional: true });
    return validateSettings(overrides || {});
}

async function writeSettings(dataDir, settings) {
    await fs.mkdir(dataDir, { recursive: true });
    const destination = path.join(dataDir, 'dashboard-runtime-overrides.json');
    const temporary = path.join(dataDir, `.dashboard-runtime-overrides.${process.pid}.${randomUUID()}.tmp`);
    try {
        const handle = await fs.open(temporary, 'wx', 0o600);
        try {
            await handle.writeFile(`${JSON.stringify(settings, null, 2)}\n`, 'utf8');
            await handle.sync();
        } finally {
            await handle.close();
        }
        await fs.rename(temporary, destination);
    } catch (error) {
        await fs.rm(temporary, { force: true });
        throw error;
    }
}

function isoTimestamp(value) {
    if (!value) return null;
    const text = String(value).trim().replace(' ', 'T');
    const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(text) ? text : text;
    const parsed = new Date(normalized);
    return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString();
}

function snapshotView(snapshot) {
    return {
        date: String(snapshot.date || '').slice(0, 10),
        timestamp: isoTimestamp(snapshot.timestamp),
        snapshot_kind: typeof snapshot.snapshot_kind === 'string' ? snapshot.snapshot_kind : null,
        equity: finite(snapshot.equity),
        cash: finite(snapshot.cash),
        invested: finite(snapshot.invested),
        position_count: finite(snapshot.position_count),
        daily_pnl: finite(snapshot.daily_pnl),
        daily_pnl_pct: finite(snapshot.daily_pnl_pct),
    };
}

function calculateMetrics(history) {
    if (!history.length) {
        return { starting_equity: null, equity: null, total_pnl: null, total_return_pct: null, daily_pnl: null, daily_pnl_pct: null, max_drawdown_pct: null, sharpe_ratio: null };
    }
    const valid = history.filter(item => item.equity !== null && item.equity > 0);
    if (!valid.length) {
        return { starting_equity: null, equity: null, total_pnl: null, total_return_pct: null, daily_pnl: null, daily_pnl_pct: null, max_drawdown_pct: null, sharpe_ratio: null };
    }
    const first = valid[0];
    const latest = valid[valid.length - 1];
    let peak = valid[0].equity;
    let maxDrawdown = 0;
    for (const item of valid) {
        peak = Math.max(peak, item.equity);
        maxDrawdown = Math.min(maxDrawdown, (item.equity / peak - 1) * 100);
    }
    const returns = valid.slice(1).map((item, index) => (item.equity / valid[index].equity - 1) * 100).filter(Number.isFinite);
    let sharpe = null;
    if (returns.length >= 3) {
        const mean = returns.reduce((sum, value) => sum + value, 0) / returns.length;
        const variance = returns.reduce((sum, value) => sum + (value - mean) ** 2, 0) / returns.length;
        if (variance > 0) sharpe = mean / Math.sqrt(variance) * Math.sqrt(252);
    }
    return {
        starting_equity: first.equity,
        equity: latest.equity,
        total_pnl: latest.equity - first.equity,
        total_return_pct: (latest.equity / first.equity - 1) * 100,
        daily_pnl: latest.daily_pnl,
        daily_pnl_pct: latest.daily_pnl_pct,
        max_drawdown_pct: maxDrawdown,
        sharpe_ratio: sharpe,
    };
}

async function readSnapshotHistory(dataDir, accountId) {
    const directory = path.join(dataDir, 'snapshots');
    let names;
    try { names = await fs.readdir(directory); }
    catch (error) { if (error.code === 'ENOENT') return []; throw error; }
    const files = names.filter(name => /^\d{4}-\d{2}-\d{2}.*\.json$/.test(name)).sort().slice(-370);
    const snapshots = await Promise.all(files.map(async name => {
        try { return await readJson(path.join(directory, name)); }
        catch { return null; }
    }));
    const matching = snapshots.filter(item => item && item.trading_mode === 'algo' && (!accountId || item.account_id === accountId));
    const closed = matching.filter(item => !item.snapshot_kind || item.snapshot_kind === 'close');
    return (closed.length ? closed : matching).map(snapshotView)
        .filter(item => item.date && item.equity !== null)
        .sort((a, b) => (a.timestamp || a.date).localeCompare(b.timestamp || b.date));
}

function summarizeResearch(report, fallbackDate = null) {
    if (!report) return null;
    const forward = safeObject(report.forward_validation);
    const portfolio = safeObject(safeObject(safeObject(report.fitness_timeline).current).portfolio);
    const orchestration = safeObject(report.orchestration);
    const adaptive = safeObject(report.adaptive_research);
    const factor = safeObject(report.factor_model);
    const promoted = Array.isArray(forward.promoted) ? forward.promoted.length : 0;
    const staged = Array.isArray(forward.staged_candidate_ids) ? forward.staged_candidate_ids.length : 0;
    const rejected = Array.isArray(forward.rejected_candidate_ids) ? forward.rejected_candidate_ids.length : 0;
    const factorOnly = orchestration.factor_only === true;
    const discovery = safeObject(adaptive.discovery);
    const verification = safeObject(adaptive.verification);
    return {
        date: String(report.run_date_et || report.created_at || fallbackDate || '').slice(0, 10),
        created_at: isoTimestamp(report.created_at),
        status: String(report.status || 'unknown'),
        active_trials: finite(forward.active_trial_count),
        promoted,
        staged,
        rejected,
        portfolio_return_pct: finite(portfolio.compounded_fold_return_pct),
        portfolio_status: String(portfolio.status || 'unavailable'),
        duration_seconds: finite(safeObject(report.stage_timings).total_seconds),
        factor_only: factorOnly,
        factor_model: factorOnly ? {
            family: String(factor.model_family || 'factor model'),
            observations: finite(factor.observation_count),
            rolling_evaluations: finite(factor.rolling_evaluation_count),
            training_samples: finite(factor.current_training_sample_count),
            forward_horizon_sessions: finite(factor.forward_horizon_sessions),
            fundamental_data_status: String(factor.fundamental_data_status || 'unavailable'),
        } : null,
        discovery_status: String(discovery.status || 'unavailable'),
        discovery_reason: String(discovery.reason || ''),
        verification_status: String(verification.status || 'unavailable'),
        verification_reason: String(verification.reason || ''),
        result: promoted ? `${promoted} promoted` : staged ? `${staged} staged` : factorOnly ? 'Factor-only model run' : String(report.status) === 'completed' ? 'No candidate promoted' : String(report.status || 'Unknown'),
    };
}

async function readResearch(dataDir) {
    const directory = path.join(dataDir, 'research', 'algo-auto');
    let names;
    try { names = await fs.readdir(directory); }
    catch (error) { if (error.code === 'ENOENT') return { latest: null, history: [] }; throw error; }
    const dates = names.filter(name => /^\d{4}-\d{2}-\d{2}\.json$/.test(name)).sort().slice(-90);
    const history = [];
    for (const name of dates) {
        const reportPath = path.join(directory, name);
        try {
            const stat = await fs.stat(reportPath);
            const cached = researchSummaryCache.get(reportPath);
            if (cached && cached.mtimeMs === stat.mtimeMs && cached.size === stat.size) {
                history.push(cached.summary);
            } else {
                const summary = summarizeResearch(await readJson(reportPath), name.slice(0, 10));
                researchSummaryCache.set(reportPath, { mtimeMs: stat.mtimeMs, size: stat.size, summary });
                history.push(summary);
            }
        } catch { /* One corrupt archived report does not hide the rest. */ }
    }
    let latestReport = null;
    const latestPath = path.join(directory, 'latest.json');
    try {
        const stat = await fs.stat(latestPath);
        const cached = researchSummaryCache.get(latestPath);
        if (cached && cached.mtimeMs === stat.mtimeMs && cached.size === stat.size) latestReport = cached.summary;
        else {
            const report = await readJson(latestPath);
            latestReport = summarizeResearch(report);
            researchSummaryCache.set(latestPath, { mtimeMs: stat.mtimeMs, size: stat.size, summary: latestReport });
        }
    }
    catch (error) { if (error.code !== 'ENOENT') latestReport = null; }
    if (!latestReport && history.length) latestReport = history[history.length - 1] || null;
    return { latest: latestReport, history: history.reverse() };
}

async function readJournal(dataDir, accountId) {
    const filePath = path.join(dataDir, 'trade_journal.jsonl');
    let handle;
    try { handle = await fs.open(filePath, 'r'); }
    catch (error) { if (error.code === 'ENOENT') return []; throw error; }
    try {
        const stat = await handle.stat();
        const length = Math.min(stat.size, 2 * 1024 * 1024);
        const buffer = Buffer.alloc(length);
        await handle.read(buffer, 0, length, stat.size - length);
        let lines = buffer.toString('utf8').split(/\r?\n/);
        if (stat.size > length) lines = lines.slice(1);
        const rows = [];
        for (const line of lines.slice(-300)) {
            try {
                const row = JSON.parse(line);
                if (row && row.trading_mode === 'algo' && (!accountId || row.account_id === accountId)) rows.push(row);
            } catch { /* Ignore a partial or malformed append-only row. */ }
        }
        return rows;
    } finally {
        await handle.close();
    }
}

function activityView(state, journal, research) {
    const portfolio = safeObject(state.strategy_portfolio);
    const activities = [];
    for (const row of journal) {
        const ticker = String(row.ticker || row.symbol || '').toUpperCase();
        if (!ticker) continue;
        const action = String(row.action || row.side || 'order').toUpperCase();
        const status = String(row.status || row.broker_status || 'recorded').toLowerCase();
        const strategy = String(row.strategy || 'strategy');
        activities.push({
            at: isoTimestamp(row.timestamp),
            kind: 'order',
            title: `${action} order · ${ticker}`,
            detail: `${strategy} · ${status}${row.qty ? ` · ${row.qty} shares` : ''}`,
            status,
        });
    }
    const scanAt = isoTimestamp(portfolio.last_scan_at);
    if (scanAt) activities.push({
        at: scanAt,
        kind: 'scan',
        title: 'Portfolio scan',
        detail: `${portfolio.last_scan_status || 'status unavailable'} · ${Array.isArray(portfolio.last_signals) ? portfolio.last_signals.length : 0} signals · ${Array.isArray(portfolio.active_strategies) ? portfolio.active_strategies.length : 0} active strategies`,
        status: portfolio.entries_paused ? 'entries paused' : 'complete',
    });
    for (const signal of Array.isArray(portfolio.last_signals) ? portfolio.last_signals : []) {
        const ticker = String(signal.ticker || signal.symbol || '').toUpperCase();
        const side = String(signal.action || signal.side || signal.direction || 'signal').toUpperCase();
        activities.push({
            at: scanAt,
            kind: 'signal',
            title: `${side} signal${ticker ? ` · ${ticker}` : ''}`,
            detail: `${signal.strategy || 'Algo strategy'}${signal.reason || signal.reasoning ? ` · ${String(signal.reason || signal.reasoning).slice(0, 110)}` : ''}`,
            status: 'signal',
        });
    }
    if (research?.latest?.created_at) activities.push({
        at: research.latest.created_at,
        kind: 'research',
        title: 'Autoresearch completed',
        detail: `${research.latest.result} · ${research.latest.status}`,
        status: research.latest.status,
    });
    return activities.filter(item => item.at).sort((a, b) => b.at.localeCompare(a.at)).slice(0, 24);
}

async function buildPayload(root) {
    const dataDir = path.join(root, 'data');
    const state = await readJson(path.join(dataDir, 'algo_state.json'));
    if (!state.strategy_portfolio || typeof state.strategy_portfolio !== 'object') throw new Error('Algo coordinator state is incomplete');
    const portfolio = safeObject(state.strategy_portfolio);
    const settings = await loadSettings(dataDir);
    const [history, journal, research] = await Promise.all([
        readSnapshotHistory(dataDir, portfolio.account_id),
        readJournal(dataDir, portfolio.account_id),
        readResearch(dataDir),
    ]);
    return {
        account: {
            mode: 'paper',
            market_state: String(state.market_state || 'UNKNOWN'),
            last_scan_at: isoTimestamp(portfolio.last_scan_at),
            last_scan_status: String(portfolio.last_scan_status || 'unknown'),
            entries_paused: typeof portfolio.entries_paused === 'boolean' ? portfolio.entries_paused : null,
            entry_pause_reasons: Array.isArray(portfolio.entry_pause_reasons) ? portfolio.entry_pause_reasons.map(reason => String(reason).slice(0, 180)) : [],
            active_strategies: Array.isArray(portfolio.active_strategies) ? portfolio.active_strategies : [],
            universe_count: Array.isArray(safeObject(portfolio.universe).tickers) ? portfolio.universe.tickers.length : 0,
        },
        performance: { history, metrics: calculateMetrics(history) },
        activity: activityView(state, journal, research),
        research,
        settings,
    };
}

module.exports = function registerAlgoTraderRoutes({ app, root = process.env.ALGO_TRADER_ROOT || '/opt/algo-trader' }) {
    app.get('/api/algo-trader', async (_req, res) => {
        try { res.json(await buildPayload(root)); }
        catch (error) {
            console.error(`Algo Trader dashboard unavailable: ${error.message}`);
            res.status(503).json({ error: 'Algo Trader data is unavailable' });
        }
    });

    app.put('/api/algo-trader/config', async (req, res) => {
        try {
            const current = await loadSettings(path.join(root, 'data'));
            const body = safeObject(req.body);
            if (!req.body || !req.body.settings || typeof req.body.settings !== 'object' || Array.isArray(req.body.settings)) throw new Error('settings must be an object');
            const settings = validateSettings({ ...current, ...body.settings });
            await writeSettings(path.join(root, 'data'), settings);
            res.json({ settings, applied: 'next portfolio scan' });
        } catch (error) {
            const isValidationError = /must be|Unsupported setting/.test(error.message);
            if (!isValidationError) console.error(`Unable to save Algo Trader settings: ${error.message}`);
            res.status(isValidationError ? 400 : 503).json({ error: isValidationError ? error.message : 'Algo Trader settings could not be saved' });
        }
    });
};

module.exports.buildPayload = buildPayload;
