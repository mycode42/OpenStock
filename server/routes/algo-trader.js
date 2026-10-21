const fs = require('node:fs/promises');
const path = require('node:path');
const { randomUUID, createHash } = require('node:crypto');

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

function summarizeOptimizer(report, fallbackId = '') {
    if (!report) return null;
    const data = safeObject(report.data);
    const adaptive = safeObject(report.adaptive_research);
    const discovery = safeObject(safeObject(adaptive.discovery).strategies);
    const strategies = Object.entries(discovery).map(([name, rawStrategy]) => {
        const strategy = safeObject(rawStrategy);
        const selected = safeObject(strategy.selected_candidate);
        const metrics = safeObject(selected.metrics);
        const attempts = Array.isArray(safeObject(strategy.window_search).attempts)
            ? strategy.window_search.attempts : [];
        const iterations = Array.isArray(strategy.search_iteration_summaries)
            ? strategy.search_iteration_summaries : [];
        const bestAttempt = attempts.reduce((best, attempt) => {
            if (!best || finite(attempt.selected_fitness) > finite(best.selected_fitness)) return attempt;
            return best;
        }, null);
        const bestIteration = iterations.reduce((best, iteration) => {
            if (!best || finite(iteration.selected_fitness) > finite(best.selected_fitness)) return iteration;
            return best;
        }, null);
        const best = bestAttempt || bestIteration;
        return {
            name,
            status: String(strategy.status || 'unknown'),
            archive_sequence: finite(strategy.archive_sequence),
            new_variants: finite(safeObject(strategy.adaptive_novelty).new_parameter_variants_on_this_data),
            search_iterations: finite(strategy.search_iterations),
            selection_status: String(strategy.selection_status || 'unavailable'),
            selected_candidate: String(selected.name || 'unavailable'),
            selected_eligible: selected.eligible === true,
            selected_return_pct: finite(metrics.net_return_pct),
            selected_closed_trades: finite(metrics.closed_trade_count),
            top_tested_candidate: String(best?.selected_candidate || ''),
            top_tested_fitness: finite(best?.selected_fitness),
            top_tested_delta: finite(best?.fitness_delta ?? best?.fitness_delta_vs_baseline),
            top_tested_window_days: finite(best?.window_days),
        };
    });
    return {
        cycle_id: String(report.invocation_id || fallbackId || report.run_id || ''),
        run_id: String(report.run_id || (report.invocation_id ? `optimization-${report.invocation_id}` : fallbackId ? `optimization-${fallbackId}` : '')),
        date: String(report.run_date_et || report.created_at || '').slice(0, 10),
        created_at: isoTimestamp(report.created_at),
        status: String(report.status || 'unknown'),
        duration_seconds: finite(safeObject(report.stage_timings).total_seconds),
        range_start: String(safeObject(data.strategy_research_range).start || data.range_start || ''),
        range_end: String(safeObject(data.strategy_research_range).end || data.research_data_end || ''),
        strategy_count: strategies.length,
        new_variants: strategies.reduce((total, strategy) => total + (strategy.new_variants || 0), 0),
        search_iterations: strategies.reduce((total, strategy) => total + (strategy.search_iterations || 0), 0),
        archive_sequence: strategies.reduce((latest, strategy) => Math.max(latest, strategy.archive_sequence || 0), 0),
        strategies,
    };
}

async function readOptimizer(dataDir) {
    const optimizerDir = path.join(dataDir, 'research', 'algo-auto', 'optimizer');
    const cyclesDir = path.join(optimizerDir, 'cycles');
    const entries = [];
    try {
        const names = await fs.readdir(cyclesDir);
        for (const name of names.filter(value => /^[a-f0-9]{32}\.json$/i.test(value))) {
            const reportPath = path.join(cyclesDir, name);
            try {
                const stat = await fs.stat(reportPath);
                entries.push({ name, reportPath, mtimeMs: stat.mtimeMs, size: stat.size });
            } catch { /* A cycle may be rotated while the directory is read. */ }
        }
    } catch (error) {
        if (error.code !== 'ENOENT') throw error;
    }
    entries.sort((a, b) => b.mtimeMs - a.mtimeMs);
    const history = [];
    for (const entry of entries.slice(0, 100)) {
        try {
            const cached = researchSummaryCache.get(entry.reportPath);
            const summary = cached && cached.mtimeMs === entry.mtimeMs && cached.size === entry.size
                ? cached.summary
                : summarizeOptimizer(await readJson(entry.reportPath), entry.name.slice(0, -5));
            if (!cached || cached.mtimeMs !== entry.mtimeMs || cached.size !== entry.size) {
                researchSummaryCache.set(entry.reportPath, {
                    mtimeMs: entry.mtimeMs, size: entry.size, summary,
                });
            }
            history.push(summary);
        } catch { /* One incomplete or corrupt report does not hide older cycles. */ }
    }
    const latestPath = path.join(optimizerDir, 'latest.json');
    try {
        const stat = await fs.stat(latestPath);
        const cached = researchSummaryCache.get(latestPath);
        const latest = cached && cached.mtimeMs === stat.mtimeMs && cached.size === stat.size
            ? cached.summary
            : summarizeOptimizer(await readJson(latestPath));
        if (!cached || cached.mtimeMs !== stat.mtimeMs || cached.size !== stat.size) {
            researchSummaryCache.set(latestPath, { mtimeMs: stat.mtimeMs, size: stat.size, summary: latest });
        }
        if (latest && !history.some(item => item.cycle_id && item.cycle_id === latest.cycle_id)) {
            history.push(latest);
        }
    } catch (error) { if (error.code !== 'ENOENT') { /* Ignore a malformed latest pointer. */ } }
    history.sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')));
    return { latest: history[0] || null, history };
}

async function readResearch(dataDir) {
    const directory = path.join(dataDir, 'research', 'algo-auto');
    let names = [];
    try { names = await fs.readdir(directory); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
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
    return { latest: latestReport, history: history.reverse(), optimizer: await readOptimizer(dataDir) };
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

function mergeConfig(base, override) {
    const result = { ...base };
    for (const [key, value] of Object.entries(safeObject(override))) {
        result[key] = value && typeof value === 'object' && !Array.isArray(value)
            && base[key] && typeof base[key] === 'object' && !Array.isArray(base[key])
            ? mergeConfig(base[key], value)
            : value;
    }
    return result;
}

function validateConfigShape(value, template, prefix = '') {
    if (template && typeof template === 'object' && !Array.isArray(template)) {
        if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(`${prefix || 'settings'} must be an object`);
        for (const [key, item] of Object.entries(value)) {
            if (!Object.hasOwn(template, key)) {
                if (prefix === 'dividends') {
                    if (typeof item !== 'number' || !Number.isFinite(item) || item < 0) throw new Error(`dividends.${key} must be a finite non-negative number`);
                    continue;
                }
                throw new Error(`Unsupported setting: ${prefix ? `${prefix}.` : ''}${key}`);
            }
            validateConfigShape(item, template[key], prefix ? `${prefix}.${key}` : key);
        }
        return;
    }
    if (Array.isArray(template)) {
        if (!Array.isArray(value) || value.some(item => typeof item !== typeof template[0])) throw new Error(`${prefix} must be an array of ${typeof template[0]}`);
        if (value.some(item => typeof item === 'number' && !Number.isFinite(item))) throw new Error(`${prefix} values must be finite`);
        return;
    }
    if (typeof template === 'number') {
        if (typeof value !== 'number' || !Number.isFinite(value)) throw new Error(`${prefix} must be a finite number`);
        return;
    }
    if (typeof value !== typeof template) throw new Error(`${prefix} must be ${typeof template}`);
}

function validateOptionsConfig(config) {
    const nonNegative = [
        ['risk', 'max_risk_per_trade_pct'], ['risk', 'max_total_defined_risk_pct'], ['risk', 'max_daily_loss_pct'],
        ['risk', 'max_delta_notional_pct'], ['risk', 'max_vega_per_vol_point_pct'], ['risk', 'max_gamma_1pct_loss_pct'],
        ['risk', 'max_theta_loss_per_day_pct'], ['risk', 'max_margin_pct'], ['vrp', 'min_iv_minus_rv'],
        ['vrp', 'short_delta_min'], ['vrp', 'short_delta_max'], ['vrp', 'wing_delta_max'],
        ['event_driven', 'min_move_edge_pct'], ['event_driven', 'min_llm_confidence'],
    ];
    for (const [section, key] of nonNegative) {
        if (config[section][key] < 0) throw new Error(`${section}.${key} must be non-negative`);
    }
    if (config.market_data_feed !== 'opra' && config.market_data_feed !== 'indicative') throw new Error('market_data_feed must be opra or indicative');
    if (config.risk.max_risk_per_trade_pct <= 0 || config.risk.max_contracts_per_trade < 1) throw new Error('risk budget and max_contracts_per_trade must be positive');
    if (config.vrp.short_delta_min > config.vrp.short_delta_max || config.vrp.max_dte < config.vrp.min_dte) throw new Error('VRP ranges are inverted');
    for (const [key, value] of Object.entries(config.regime_risk_multipliers || {})) {
        if (!Number.isFinite(value) || value < 0 || value > 2) throw new Error(`regime_risk_multipliers.${key} must be between 0 and 2`);
    }
    return config;
}

function optionsConfigPath(root) {
    const overridePath = process.env.ALPACA_OPTIONS_CONFIG;
    return overridePath ? path.resolve(root, overridePath) : path.join(root, 'data', 'options', 'config.json');
}

async function loadOptionsSettings(root) {
    const defaults = await readJson(path.join(root, 'options', 'config.default.json'));
    const override = await readJson(optionsConfigPath(root), { optional: true }) || {};
    const settings = mergeConfig(defaults, override);
    if (process.env.ALPACA_OPTIONS_FEED) settings.market_data_feed = process.env.ALPACA_OPTIONS_FEED.trim().toLowerCase();
    validateConfigShape(settings, defaults);
    return validateOptionsConfig(settings);
}

async function writeOptionsSettings(root, settings) {
    const defaults = await readJson(path.join(root, 'options', 'config.default.json'));
    const merged = mergeConfig(await loadOptionsSettings(root), settings);
    validateConfigShape(merged, defaults);
    validateOptionsConfig(merged);
    const destination = optionsConfigPath(root);
    await fs.mkdir(path.dirname(destination), { recursive: true, mode: 0o700 });
    const temporary = `${destination}.${process.pid}.${randomUUID()}.tmp`;
    try {
        const handle = await fs.open(temporary, 'wx', 0o600);
        try {
            await handle.writeFile(`${JSON.stringify(merged, null, 2)}\n`, 'utf8');
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

async function readJsonLines(filePath, limit = 500) {
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
        for (const line of lines.slice(-limit)) {
            try { const row = JSON.parse(line); if (row && typeof row === 'object' && !Array.isArray(row)) rows.push(row); }
            catch { /* Ignore a partial or malformed append-only row. */ }
        }
        return rows;
    } finally {
        await handle.close();
    }
}

async function optionsAccountDirectory(dataDir) {
    const accountId = String(process.env.ALPACA_OPTIONS_ACCOUNT_ID || '').trim();
    if (accountId) {
        const digest = createHash('sha256').update(accountId).digest('hex').slice(0, 16);
        return path.join(dataDir, 'options', 'accounts', digest);
    }
    const directory = path.join(dataDir, 'options', 'accounts');
    try {
        const names = (await fs.readdir(directory, { withFileTypes: true }))
            .filter(entry => entry.isDirectory() && /^[a-f0-9]{16}$/.test(entry.name))
            .map(entry => entry.name);
        return names.length === 1 ? path.join(directory, names[0]) : null;
    } catch (error) {
        if (error.code === 'ENOENT') return null;
        throw error;
    }
}

function summarizeOptionsResearch(report) {
    if (!report) return null;
    const strategies = Object.entries(safeObject(report.strategies)).map(([name, row]) => ({
        name,
        status: String(row.status || 'unknown'),
        approved: row.approved === true,
        underlying: String(row.underlying || ''),
        data_mode: String(row.data_mode || ''),
        walk_forward: safeObject(row.walk_forward),
        holdout: safeObject(row.holdout),
    }));
    const approved = strategies.filter(row => row.approved).length;
    const completed = strategies.filter(row => row.status === 'completed').length;
    const result = strategies.map(row => `${row.name.replaceAll('_', ' ')} · ${row.status}`).join(' / ')
        || String(report.reason || report.status || 'No options research results');
    return {
        date: String(report.run_date_et || report.created_at || '').slice(0, 10),
        created_at: isoTimestamp(report.created_at),
        status: String(report.status || 'unknown'),
        result,
        active_trials: strategies.length,
        promoted: approved,
        staged: Math.max(0, completed - approved),
        rejected: strategies.length - completed,
        portfolio_return_pct: null,
        portfolio_status: 'research only',
        options_strategies: strategies,
    };
}

async function readOptionsResearch(dataDir) {
    const directory = path.join(dataDir, 'options', 'research', 'automation');
    let names;
    try { names = await fs.readdir(directory); }
    catch (error) { if (error.code === 'ENOENT') return { latest: null, history: [] }; throw error; }
    const dated = names.filter(name => /^\d{4}-\d{2}-\d{2}\.json$/.test(name)).sort().slice(-90);
    const history = [];
    for (const name of dated) {
        try { history.push(summarizeOptionsResearch(await readJson(path.join(directory, name)))); }
        catch { /* One malformed research archive does not hide the rest. */ }
    }
    let latest = null;
    try { latest = summarizeOptionsResearch(await readJson(path.join(directory, 'latest.json'))); }
    catch { /* The latest report is optional until options research runs. */ }
    if (!latest && history.length) latest = history[history.length - 1];
    if (latest && !history.some(item => item?.date === latest.date)) history.push(latest);
    return { latest, history: history.filter(Boolean).sort((a, b) => b.date.localeCompare(a.date)) };
}

function optionsSnapshotView(row, previous) {
    const equity = finite(row.options_equity ?? safeObject(row.options_account_summary).options_equity);
    const previousEquity = finite(previous?.options_equity ?? safeObject(previous?.options_account_summary).options_equity);
    const dailyPnl = equity !== null && previousEquity !== null ? equity - previousEquity : null;
    return {
        date: String(row.logged_at || '').slice(0, 10),
        timestamp: isoTimestamp(row.logged_at),
        snapshot_kind: 'options',
        equity,
        cash: null,
        invested: null,
        position_count: Array.isArray(row.open_option_positions) ? row.open_option_positions.length : 0,
        daily_pnl: dailyPnl,
        daily_pnl_pct: dailyPnl !== null && previousEquity > 0 ? dailyPnl / previousEquity * 100 : null,
    };
}

function optionsActivityView(decisions, orders, assignments, research) {
    const activities = [];
    for (const row of decisions) {
        const signal = safeObject(row.signal);
        const underlying = String(signal.underlying || '').toUpperCase();
        const strategy = String(signal.strategy || 'Options strategy').replaceAll('_', ' ');
        activities.push({
            at: isoTimestamp(row.logged_at), kind: 'signal',
            title: `${row.approved === true ? 'Approved' : 'Blocked'} options signal${underlying ? ` · ${underlying}` : ''}`,
            detail: `${strategy}${row.reason ? ` · ${String(row.reason).slice(0, 110)}` : ''}`,
            status: row.approved === true ? 'approved' : 'rejected',
        });
    }
    for (const row of orders) {
        const signal = safeObject(row.signal);
        const underlying = String(signal.underlying || '').toUpperCase();
        const status = String(row.status || safeObject(row.broker_response).status || 'recorded').toLowerCase();
        activities.push({
            at: isoTimestamp(row.logged_at), kind: 'order',
            title: `${status === 'dry_run' ? 'Paper order preview' : 'Options order'}${underlying ? ` · ${underlying}` : ''}`,
            detail: `${String(signal.strategy || 'Options strategy').replaceAll('_', ' ')} · ${status}`,
            status,
        });
    }
    for (const row of assignments) {
        const event = safeObject(row.broker_activity || row.simulation);
        activities.push({
            at: isoTimestamp(row.logged_at), kind: 'signal',
            title: 'Assignment activity',
            detail: String(event.type || event.symbol || 'Options assignment recorded').slice(0, 120),
            status: 'recorded',
        });
    }
    if (research?.latest?.created_at) activities.push({
        at: research.latest.created_at, kind: 'research', title: 'Options research completed',
        detail: `${research.latest.result} · ${research.latest.status}`, status: research.latest.status,
    });
    return activities.filter(item => item.at).sort((a, b) => b.at.localeCompare(a.at)).slice(0, 24);
}

async function buildOptionsPayload(root) {
    const dataDir = path.join(root, 'data');
    const settings = await loadOptionsSettings(root);
    const accountDir = await optionsAccountDirectory(dataDir);
    const [riskRows, decisions, orders, assignments, research] = await Promise.all([
        accountDir ? readJsonLines(path.join(accountDir, 'risk_daily.jsonl')) : [],
        accountDir ? readJsonLines(path.join(accountDir, 'decisions.jsonl')) : [],
        accountDir ? readJsonLines(path.join(accountDir, 'orders.jsonl')) : [],
        accountDir ? readJsonLines(path.join(accountDir, 'assignment_events.jsonl')) : [],
        readOptionsResearch(dataDir),
    ]);
    const chronologicalRiskRows = [...riskRows].sort((a, b) => String(a.logged_at || '').localeCompare(String(b.logged_at || '')));
    const history = [];
    let currentDay = '';
    let lastRowOfDay = null;
    let previousDayRow = null;
    for (const row of chronologicalRiskRows) {
        const day = String(row.logged_at || '').slice(0, 10);
        if (currentDay && day !== currentDay) previousDayRow = lastRowOfDay;
        if (day !== currentDay) currentDay = day;
        const point = optionsSnapshotView(row, previousDayRow);
        if (point.date && point.equity !== null) history.push(point);
        lastRowOfDay = row;
    }
    const latestRisk = riskRows[riskRows.length - 1] || {};
    const activeStrategies = [...new Set(decisions.slice(-50).map(row => safeObject(row.signal).strategy).filter(Boolean))];
    const universe = [...new Set(decisions.slice(-50).map(row => safeObject(row.signal).underlying).filter(Boolean))];
    const disabled = !settings.enabled;
    const unconfigured = !accountDir;
    const entryPauseReasons = [];
    if (disabled) entryPauseReasons.push('Options paper route is disabled in options configuration');
    if (unconfigured) entryPauseReasons.push('No options paper account journal was found');
    const latestTimestamp = isoTimestamp(latestRisk.logged_at);
    return {
        account: {
            mode: 'paper',
            market_state: String(latestRisk.regime || latestRisk.volatility_state || 'OPTIONS PAPER'),
            last_scan_at: latestTimestamp,
            last_scan_status: latestTimestamp ? 'complete' : 'no options scan recorded',
            entries_paused: disabled || unconfigured ? true : null,
            entry_pause_reasons: entryPauseReasons,
            active_strategies: activeStrategies,
            universe_count: universe.length,
        },
        performance: { history, metrics: calculateMetrics(history) },
        activity: optionsActivityView(decisions, orders, assignments, research),
        research,
        settings,
        settings_readonly_fields: process.env.ALPACA_OPTIONS_FEED ? ['market_data_feed'] : [],
        account_type: 'options',
    };
}

module.exports = function registerAlgoTraderRoutes({ app, root = process.env.ALGO_TRADER_ROOT || '/opt/algo-trader' }) {
    app.get('/api/algo-trader', async (req, res) => {
        const account = req.query?.account || 'equity';
        if (!['equity', 'options'].includes(account)) return res.status(400).json({ error: 'Unsupported Algo Trader account' });
        try { res.json(account === 'options' ? await buildOptionsPayload(root) : await buildPayload(root)); }
        catch (error) {
            console.error(`Algo Trader dashboard unavailable: ${error.message}`);
            res.status(503).json({ error: 'Algo Trader data is unavailable' });
        }
    });

    app.put('/api/algo-trader/config', async (req, res) => {
        const account = req.query?.account || req.body?.account || 'equity';
        if (!['equity', 'options'].includes(account)) return res.status(400).json({ error: 'Unsupported Algo Trader account' });
        try {
            if (account === 'options') {
                if (!req.body || !req.body.settings || typeof req.body.settings !== 'object' || Array.isArray(req.body.settings)) throw new Error('settings must be an object');
                await writeOptionsSettings(root, req.body.settings);
                res.json({ settings: await loadOptionsSettings(root), applied: 'next options paper scan' });
                return;
            }
            const current = await loadSettings(path.join(root, 'data'));
            const body = safeObject(req.body);
            if (!req.body || !req.body.settings || typeof req.body.settings !== 'object' || Array.isArray(req.body.settings)) throw new Error('settings must be an object');
            const settings = validateSettings({ ...current, ...body.settings });
            await writeSettings(path.join(root, 'data'), settings);
            res.json({ settings, applied: 'next portfolio scan' });
        } catch (error) {
            const isValidationError = /must be|Unsupported setting|Unsupported Algo Trader account|inverted|positive|between/.test(error.message);
            if (!isValidationError) console.error(`Unable to save Algo Trader settings: ${error.message}`);
            res.status(isValidationError ? 400 : 503).json({ error: isValidationError ? error.message : 'Algo Trader settings could not be saved' });
        }
    });
};

module.exports.buildPayload = buildPayload;
