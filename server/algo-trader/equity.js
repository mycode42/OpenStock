const fs = require('node:fs/promises');
const path = require('node:path');
const Database = require('better-sqlite3');
const { finite, isoTimestamp, readCoordinatorState, readJson, readJsonLines, safeObject } = require('./io');
const { attachEuroAmounts, cachedEquityFillReport, equityStrategyPerformance, readUsdEurRate } = require('./performance');
const { loadSettings } = require('./settings');

const researchSummaryCache = new Map();
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
    const daily = [];
    for (const item of valid) {
        const day = String(item.date || item.timestamp || '').slice(0, 10);
        if (daily.length && daily[daily.length - 1].day === day) daily[daily.length - 1] = { day, equity: item.equity };
        else daily.push({ day, equity: item.equity });
    }
    const returns = daily.slice(1).map((item, index) => (item.equity / daily[index].equity - 1) * 100).filter(Number.isFinite);
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

async function readSnapshotHistory(dataDir, accountId, { tradingMode = 'algo' } = {}) {
    // Snapshots are rows in the algo state database, one per point; the server reads them read-only.
    const databasePath = path.join(dataDir, 'algo_state.sqlite3');
    let db;
    try { db = new Database(databasePath, { readonly: true, fileMustExist: true }); }
    catch (error) { if (error.code === 'SQLITE_CANTOPEN') return []; throw error; }
    try {
        const sql = accountId
            ? 'SELECT payload_json FROM account_snapshots WHERE trading_mode = ? AND account_id = ? ORDER BY taken_at'
            : 'SELECT payload_json FROM account_snapshots WHERE trading_mode = ? ORDER BY taken_at';
        const rows = db.prepare(sql).all(...(accountId ? [tradingMode, accountId] : [tradingMode]));
        return rows.map(({ payload_json }) => {
            try { return JSON.parse(payload_json); }
            catch { return null; }
        })
            .filter(Boolean)
            .map(snapshotView)
            .filter(item => item.date && item.equity !== null)
            .sort((a, b) => (a.timestamp || a.date).localeCompare(b.timestamp || b.date));
    } catch (error) {
        if (/no such table/.test(error.message)) return [];
        throw error;
    } finally {
        db.close();
    }
}

function summarizeResearch(report, fallbackDate = null) {
    if (!report) return null;
    const forward = safeObject(report.forward_validation);
    const portfolio = safeObject(safeObject(safeObject(report.fitness_timeline).current).portfolio);
    const fitnessTimeline = safeObject(report.fitness_timeline);
    const fitnessStrategies = safeObject(safeObject(fitnessTimeline.current).strategies);
    const comparison = safeObject(fitnessTimeline.comparison_to_previous);
    const strategyChanges = safeObject(comparison.strategy_changes);
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
        strategy_fitness: Object.entries(fitnessStrategies).map(([name, strategy]) => {
            const verification = safeObject(safeObject(strategy).verification);
            const change = safeObject(strategyChanges[name]);
            const delta = comparison.available === true && comparison.comparable === true
                ? finite(change.oos_fitness_delta)
                : null;
            return {
                name,
                fitness: finite(verification.fitness),
                delta_vs_previous: delta,
                previous_run_date: comparison.prior_run_date_et || null,
            };
        }),
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
    // The Algo state database holds the journal; read the newest algo rows, oldest first.
    const databasePath = path.join(dataDir, 'algo_state.sqlite3');
    let db;
    try { db = new Database(databasePath, { readonly: true, fileMustExist: true }); }
    catch (error) { if (error.code === 'SQLITE_CANTOPEN') return []; throw error; }
    try {
        const sql = accountId
            ? "SELECT payload_json FROM trade_journal WHERE trading_mode = 'algo' AND account_id = ? ORDER BY id DESC LIMIT 300"
            : "SELECT payload_json FROM trade_journal WHERE trading_mode = 'algo' ORDER BY id DESC LIMIT 300";
        const rows = db.prepare(sql).all(...(accountId ? [accountId] : []));
        return rows.reverse().map(({ payload_json }) => {
            try { return JSON.parse(payload_json); }
            catch { return null; }
        }).filter(Boolean);
    } catch (error) {
        if (/no such table/.test(error.message)) return [];
        throw error;
    } finally {
        db.close();
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
    const state = await readCoordinatorState(root);
    if (!state.strategy_portfolio || typeof state.strategy_portfolio !== 'object') throw new Error('Algo coordinator state is incomplete');
    const portfolio = safeObject(state.strategy_portfolio);
    const settings = await loadSettings(root);
    const fillReport = cachedEquityFillReport(root, portfolio.account_id);
    const [history, journal, research] = await Promise.all([
        readSnapshotHistory(dataDir, portfolio.account_id),
        readJournal(dataDir, portfolio.account_id),
        readResearch(dataDir),
    ]);
    const strategyPerformance = equityStrategyPerformance(fillReport);
    const hasStrategyPnl = strategyPerformance.strategies.some(strategy => (
        strategy.latest_pnl_usd !== null || strategy.cumulative_pnl_usd !== null
    ));
    const fx = hasStrategyPnl ? await readUsdEurRate() : null;
    // The saved flag is the last scan's result and is not rewritten at the close, so a closed market is reported here, as for Options.
    const marketClosed = String(state.market_state || '').toUpperCase() === 'CLOSED';
    const entryPauseReasons = Array.isArray(portfolio.entry_pause_reasons) ? portfolio.entry_pause_reasons.map(reason => String(reason).slice(0, 180)) : [];
    if (marketClosed) entryPauseReasons.push('Equity market is closed');
    return {
        account: {
            mode: 'paper',
            market_state: String(state.market_state || 'UNKNOWN'),
            market_status: String(state.market_state || 'UNKNOWN'),
            regime: String(safeObject(portfolio.market_regime).regime || 'unknown'),
            volatility_state: String(safeObject(portfolio.market_regime).volatility_state || 'unknown'),
            regime_engine: String(safeObject(portfolio.market_regime).regime_engine || 'unknown'),
            last_scan_at: isoTimestamp(portfolio.last_scan_at),
            last_scan_status: String(portfolio.last_scan_status || 'unknown'),
            entries_paused: marketClosed || (typeof portfolio.entries_paused === 'boolean' ? portfolio.entries_paused : null),
            entry_pause_reasons: entryPauseReasons,
            active_strategies: Array.isArray(portfolio.active_strategies) ? portfolio.active_strategies : [],
            universe_count: Array.isArray(safeObject(portfolio.universe).tickers) ? portfolio.universe.tickers.length : 0,
        },
        performance: { history, metrics: calculateMetrics(history) },
        strategy_performance: attachEuroAmounts(strategyPerformance, fx),
        activity: activityView(state, journal, research),
        research,
        settings,
    };
}

module.exports = {
    calculateMetrics,
    snapshotView,
    readSnapshotHistory,
    summarizeResearch,
    summarizeOptimizer,
    readOptimizer,
    readResearch,
    researchSummaryCache,
    readJournal,
    activityView,
    buildPayload,
};
