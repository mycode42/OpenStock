const fs = require('node:fs/promises');
const path = require('node:path');
const { randomUUID, createHash } = require('node:crypto');
const { calculateMetrics } = require('./equity');
const { finite, isoTimestamp, readCoordinatorState, readJson, readJsonLines, safeObject } = require('./io');
const { attachEuroAmounts, optionsStrategyPerformance, readUsdEurRate } = require('./performance');
const { loadOptionsSettings } = require('./settings');

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

function optionsStrategiesForRegime(settings, regime) {
    const currentRegime = String(regime || '').trim().toLowerCase();
    if (!currentRegime) return [];

    const globalWeights = safeObject(settings.regime_risk_multipliers);
    const regimeWeight = config => {
        const weights = { ...globalWeights, ...safeObject(safeObject(config).regime_risk_multipliers) };
        const value = finite(weights[currentRegime]);
        return value !== null && value > 0;
    };
    const strategies = [];
    const longVolatility = safeObject(settings.long_volatility);
    const exclusiveLongVolatility = longVolatility.enabled === true && longVolatility.mode === 'exclusive';

    if (!exclusiveLongVolatility && safeObject(settings.llm_driven).enabled !== false
        && regimeWeight(settings.llm_driven)) {
        strategies.push('LLM Driven');
    }

    const allowedLongVolatilityRegimes = Array.isArray(longVolatility.allowed_regimes)
        ? longVolatility.allowed_regimes.map(value => String(value).toLowerCase())
        : ['volatile'];
    if (longVolatility.enabled !== false && allowedLongVolatilityRegimes.includes(currentRegime)
        && regimeWeight(longVolatility)) {
        strategies.push('Long Volatility');
    }

    const vrp = safeObject(settings.vrp);
    const allowedVrpRegimes = Array.isArray(vrp.allowed_regimes)
        ? vrp.allowed_regimes.map(value => String(value).toLowerCase())
        : [];
    if (!exclusiveLongVolatility && vrp.enabled !== false && allowedVrpRegimes.includes(currentRegime)
        && regimeWeight(vrp)) {
        strategies.push('Volatility Risk Premium');
    }
    return strategies;
}

function optionsActivityView(decisions, orders, fills, assignments, research) {
    const activities = [];
    for (const row of decisions) {
        // Cycle summaries are stored in the decisions journal too, but they do
        // not represent signals and have no approval decision to display.
        if (!row || !row.signal || typeof row.approved !== 'boolean') continue;
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
    const finalOrderIntents = new Set(orders
        .filter(row => row?.client_order_id && row.broker_response)
        .map(row => String(row.client_order_id)));
    for (const row of orders) {
        if (row?.kind === 'cycle_summary') {
            const status = String(row.status || '').toLowerCase();
            const submitted = Number(row.submitted_count) || 0;
            activities.push({
                at: isoTimestamp(row.logged_at), kind: 'scan', title: 'Options scan',
                detail: `${row.candidate_count || 0} candidates · ${row.approved_count || 0} approved · ${row.submitted_count || 0} submitted`,
                status: submitted > 0 ? 'submitted' : status === 'cycle_no_order' ? 'no order' : 'order intent',
            });
            continue;
        }
        if (String(row?.status || '').toLowerCase() === 'submitting'
            && row.client_order_id && finalOrderIntents.has(String(row.client_order_id))) continue;
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
    const ordersByClientId = new Map(orders
        .filter(row => row?.client_order_id && row.signal)
        .map(row => [String(row.client_order_id), row]));
    for (const row of fills) {
        const order = ordersByClientId.get(String(row.client_order_id || '')) || {};
        const signal = safeObject(order.signal);
        const underlying = String(signal.underlying || '').toUpperCase();
        activities.push({
            at: isoTimestamp(row.logged_at), kind: 'order',
            title: `Options fill${underlying ? ` · ${underlying}` : ''}`,
            detail: `${String(signal.strategy || 'Options strategy').replaceAll('_', ' ')} · ${row.filled_qty || 0} contracts filled`,
            status: 'filled',
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
    const [riskRows, decisions, orders, fills, strategyGreeks, assignments, research, coordinatorState] = await Promise.all([
        accountDir ? readJsonLines(path.join(accountDir, 'risk_daily.jsonl')) : [],
        accountDir ? readJsonLines(path.join(accountDir, 'decisions.jsonl')) : [],
        accountDir ? readJsonLines(path.join(accountDir, 'orders.jsonl')) : [],
        accountDir ? readJsonLines(path.join(accountDir, 'fills.jsonl')) : [],
        accountDir ? readJsonLines(path.join(accountDir, 'strategy_greeks.jsonl')) : [],
        accountDir ? readJsonLines(path.join(accountDir, 'assignment_events.jsonl')) : [],
        readOptionsResearch(dataDir),
        readCoordinatorState(root).catch(() => ({})),
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
    const universe = [...new Set(decisions.slice(-50).map(row => safeObject(row.signal).underlying).filter(Boolean))];
    const disabled = !settings.enabled;
    const unconfigured = !accountDir;
    const coordinatorPortfolio = safeObject(coordinatorState?.strategy_portfolio);
    const sharedMarketRegime = safeObject(coordinatorPortfolio.market_regime);
    const sharedRegime = String(sharedMarketRegime.regime || 'unknown').trim().toLowerCase();
    const marketStatus = String(coordinatorState?.market_state || '').toUpperCase();
    const marketClosed = marketStatus === 'CLOSED';
    const entryPauseReasons = [];
    if (disabled) entryPauseReasons.push('Options paper route is disabled in options configuration');
    if (unconfigured) entryPauseReasons.push('No options paper account journal was found');
    if (marketClosed && !disabled && !unconfigured) entryPauseReasons.push('Options market is closed');
    if (sharedRegime === 'unknown' && !disabled && !unconfigured && !marketClosed) {
        entryPauseReasons.push('Shared Algo coordinator regime is unavailable');
    }
    const entriesPaused = disabled || unconfigured || marketClosed || sharedRegime === 'unknown';
    const activeStrategies = optionsStrategiesForRegime(settings, sharedRegime);
    const strategyPerformance = optionsStrategyPerformance(orders, fills, strategyGreeks, riskRows);
    const hasStrategyPnl = strategyPerformance.strategies.some(strategy => (
        strategy.latest_pnl_usd !== null || strategy.cumulative_pnl_usd !== null
    ));
    const fx = hasStrategyPnl ? await readUsdEurRate() : null;
    return {
        account: {
            mode: 'paper',
            market_state: marketStatus || 'UNKNOWN',
            market_status: marketStatus || 'UNKNOWN',
            regime: sharedRegime,
            volatility_state: String(sharedMarketRegime.volatility_state || 'unknown'),
            regime_engine: String(sharedMarketRegime.regime_engine || 'unknown'),
            last_scan_at: isoTimestamp(coordinatorPortfolio.last_scan_at),
            last_scan_status: String(coordinatorPortfolio.last_scan_status || 'unknown'),
            entries_paused: entriesPaused,
            entry_pause_reasons: entryPauseReasons,
            active_strategies: activeStrategies,
            active_strategies_label: 'Strategies available in this regime',
            universe_count: universe.length,
        },
        performance: { history, metrics: calculateMetrics(history) },
        strategy_performance: attachEuroAmounts(strategyPerformance, fx),
        activity: optionsActivityView(decisions, orders, fills, assignments, research),
        research,
        settings,
        settings_readonly_fields: process.env.ALPACA_OPTIONS_FEED ? ['market_data_feed'] : [],
        account_type: 'options',
    };
}

module.exports = {
    optionsAccountDirectory,
    summarizeOptionsResearch,
    readOptionsResearch,
    optionsSnapshotView,
    optionsStrategiesForRegime,
    optionsActivityView,
    buildOptionsPayload,
};
