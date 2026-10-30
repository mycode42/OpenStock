'use strict';

const UNIVERSE = Object.freeze([
    { symbol: 'AAPL', base: 269.18, category: 'large_cap', sector: 'technology' },
    { symbol: 'AMZN', base: 226.42, category: 'large_cap', sector: 'consumer_discretionary' },
    { symbol: 'AMD', base: 163.57, category: 'large_cap', sector: 'semiconductors' },
    { symbol: 'COST', base: 987.31, category: 'quality', sector: 'consumer_staples' },
    { symbol: 'GOOGL', base: 198.28, category: 'large_cap', sector: 'communication_services' },
    { symbol: 'META', base: 713.9, category: 'large_cap', sector: 'communication_services' },
    { symbol: 'MSFT', base: 521.7, category: 'large_cap', sector: 'technology' },
    { symbol: 'NVDA', base: 183.22, category: 'momentum', sector: 'semiconductors' },
]);

const HISTORY_RANGES = Object.freeze({
    '1d': { duration: 14.5 * 60 * 60_000, points: 48, drift: 0.004, volatility: 0.008 },
    '1w': { duration: 7 * 24 * 60 * 60_000, points: 56, drift: 0.018, volatility: 0.018 },
    '1mo': { duration: 30 * 24 * 60 * 60_000, points: 60, drift: 0.045, volatility: 0.035 },
    '1y': { duration: 365 * 24 * 60 * 60_000, points: 80, drift: 0.19, volatility: 0.09 },
    '5y': { duration: 1826 * 24 * 60 * 60_000, points: 96, drift: 0.62, volatility: 0.19 },
});

function seed(value) {
    return [...String(value)].reduce((sum, character) => ((sum * 31) + character.charCodeAt(0)) % 997, 17);
}

function date(value) {
    const result = value instanceof Date ? value : new Date(value);
    return Number.isNaN(result.getTime()) ? new Date() : result;
}

function demoPrice(base, symbol, now) {
    const phase = seed(symbol);
    return base * (1 + Math.sin(now.getTime() / 45_000 + phase) * 0.0007);
}

function series(base, symbol, range, now, valueField = 'price_eur') {
    const config = HISTORY_RANGES[range];
    if (!config) return null;
    const end = now.getTime();
    const start = end - config.duration;
    const phase = seed(symbol) / 997 * Math.PI * 2;
    const down = seed(`${symbol}:direction`) % 3 === 0 ? -1 : 1;
    return Array.from({ length: config.points }, (_, index) => {
        const progress = index / (config.points - 1);
        const wave = Math.sin(progress * Math.PI * 10 + phase) * Math.sin(progress * Math.PI);
        const price = base * (1 - down * config.drift + down * config.drift * progress + wave * config.volatility);
        const recordedAt = new Date(start + config.duration * progress).toISOString();
        return { [valueField]: Number(price.toFixed(4)), recorded_at: recordedAt, quote_time: recordedAt };
    });
}

function universePayload(nowValue = new Date()) {
    const now = date(nowValue);
    const timestamp = now.toISOString();
    return {
        demo_mode: true,
        selection_status: 'ready',
        selection_date: timestamp.slice(0, 10),
        selector_as_of: timestamp.slice(0, 10),
        last_scan_at: timestamp,
        market_state: 'OPEN',
        symbol_count: UNIVERSE.length,
        quote_status: 'ok',
        quote_feed: 'Simulated data',
        quotes_observed_at: timestamp,
        quote_error: null,
        symbols: UNIVERSE.map(item => {
            const midpoint = demoPrice(item.base, item.symbol, now);
            const spread = midpoint * 0.0006;
            return {
                symbol: item.symbol,
                category: item.category,
                sector: item.sector,
                quote: {
                    midpoint,
                    bid: midpoint - spread / 2,
                    ask: midpoint + spread / 2,
                    timestamp,
                },
            };
        }),
    };
}

function universeHistory(symbol, range, nowValue = new Date()) {
    const item = UNIVERSE.find(row => row.symbol === String(symbol).toUpperCase());
    if (!item || !HISTORY_RANGES[range]) return null;
    const now = date(nowValue);
    const points = series(demoPrice(item.base, item.symbol, now), item.symbol, range, now, 'price')
        .map(({ price, recorded_at }) => ({ price, recorded_at }));
    return { demo_mode: true, points, start_at: points[0].recorded_at, end_at: points[points.length - 1].recorded_at, observed_at: now.toISOString(), stale: false };
}

// Demo account history in the same shape as the recorded snapshots: one close per day, plus
// 5-minute points around the clock for the last 36 hours. The last point is the headline value.
function equityHistory(account, now) {
    const start = account === 'options' ? 38_000 : 100_000;
    const end = now.getTime();
    const positionCount = account === 'options' ? 3 : 5;
    const row = (time, kind, equity) => {
        const timestamp = new Date(time).toISOString();
        return {
            date: timestamp.slice(0, 10), timestamp, snapshot_kind: kind, equity: Number(equity.toFixed(2)),
            cash: Number((equity * 0.31).toFixed(2)), invested: Number((equity * 0.69).toFixed(2)), position_count: positionCount,
        };
    };
    // Closes stop before the intraday window, so the two series never overlap, as in the recorded history.
    const days = 118;
    const trend = progress => start * (1 + progress * 0.071 + Math.sin(progress * Math.PI * 12) * 0.012 + Math.sin(progress * Math.PI * 31) * 0.004);
    const closes = Array.from({ length: days }, (_, index) => row(end - (days + 2 - index) * 86_400_000, 'close', trend(index / (days - 1))));
    const latest = trend(1);
    const intraday = Array.from({ length: 36 * 12 }, (_, index) => {
        const time = end - (36 * 12 - 1 - index) * 5 * 60_000;
        return row(time, 'intraday', latest * (1 + Math.sin((time - end) / (4 * 3_600_000)) * 0.0015));
    });
    return [...closes, ...intraday];
}

function researchHistory(now) {
    return Array.from({ length: 5 }, (_, index) => {
        const day = new Date(now.getTime() - (4 - index) * 7 * 86_400_000);
        const dateValue = day.toISOString().slice(0, 10);
        return {
            date: dateValue,
            created_at: day.toISOString(),
            status: 'completed',
            result: ['Momentum basket validated', 'Mean-reversion parameters updated', 'Volatility filter improved', 'Risk limits reviewed', 'Weekly strategy scan complete'][index],
            portfolio_status: 'simulated',
            active_trials: 12 + index * 2,
            promoted: index === 1 ? 0 : 1,
            staged: 2,
            portfolio_return_pct: Number((1.2 + index * 0.46).toFixed(2)),
            strategy_fitness: [
                { name: 'momentum', fitness: 0.31 + index * 0.015, delta_vs_previous: 0.02 },
                { name: 'mean_reversion', fitness: 0.22 + index * 0.009, delta_vs_previous: 0.01 },
            ],
        };
    }).reverse();
}

function algoPayload(accountType = 'equity', nowValue = new Date()) {
    const now = date(nowValue);
    const history = equityHistory(accountType, now);
    const first = history[0].equity;
    const latest = history[history.length - 1].equity;
    const dailyPnl = Number((latest - history[history.length - 2].equity).toFixed(2));
    const metrics = {
        starting_equity: first,
        equity: latest,
        total_pnl: Number((latest - first).toFixed(2)),
        total_return_pct: (latest / first - 1) * 100,
        daily_pnl: dailyPnl,
        daily_pnl_pct: (dailyPnl / history[history.length - 2].equity) * 100,
        max_drawdown_pct: -1.86,
        sharpe_ratio: 1.42,
    };
    const strategies = accountType === 'options'
        ? ['options_vrp', 'options_llm_driven', 'options_directional_overlay']
        : ['momentum', 'mean_reversion', 'swing_llm_driven'];
    const scorecard = {
        status: 'ready',
        source: 'simulated demo fills',
        message: 'Example strategy results for preview only.',
        known_strategies: strategies,
        strategies: strategies.map((strategy, index) => ({
            strategy,
            complete: true,
            latest_activity: { action: index === 1 ? 'SELL' : 'BUY', symbol: ['NVDA', 'AAPL', 'MSFT'][index], at: now.toISOString(), qty: index + 2, status: 'simulated' },
            latest_pnl_eur: [42.8, -13.4, 28.1][index],
            latest_pnl_pct: [0.68, -0.22, 0.41][index],
            cumulative_pnl_eur: [286.5, 94.2, 173.8][index],
            cumulative_pnl_pct: [4.1, 1.4, 2.7][index],
        })),
    };
    const research = researchHistory(now);
    return {
        demo_mode: true,
        account: {
            mode: 'paper',
            market_state: 'OPEN', market_status: 'OPEN',
            regime: 'risk_on', volatility_state: 'normal', regime_engine: 'demo simulation',
            last_scan_at: now.toISOString(), last_scan_status: 'complete',
            entries_paused: true,
            entry_pause_reasons: ['Demo mode uses simulated data; no orders are sent.'],
            active_strategies: strategies,
            active_strategies_label: 'Example strategies',
            universe_count: UNIVERSE.length,
        },
        performance: { history, metrics },
        strategy_performance: scorecard,
        activity: [
            { at: now.toISOString(), kind: 'scan', title: 'Demo portfolio scan', detail: 'Sample market conditions · simulated only', status: 'simulated' },
            { at: new Date(now.getTime() - 22 * 60_000).toISOString(), kind: 'signal', title: 'Demo BUY signal · NVDA', detail: 'Momentum setup passed example score filters', status: 'simulated' },
            { at: new Date(now.getTime() - 51 * 60_000).toISOString(), kind: 'order', title: 'Demo paper fill · AAPL', detail: 'Mean-reversion sample · 3 shares · no broker order', status: 'simulated' },
            { at: new Date(now.getTime() - 96 * 60_000).toISOString(), kind: 'research', title: 'Demo research completed', detail: 'Weekly strategy scan · sample result', status: 'completed' },
        ],
        research: { latest: research[0], history: research, optimizer: { latest: null, history: [] } },
        settings: { strategies: Object.fromEntries(strategies.map(strategy => [strategy, {}])) },
        ...(accountType === 'options' ? { account_type: 'options', settings_readonly_fields: [] } : {}),
    };
}

// Research lab overview with no Algo Trader data behind it: the same strategy list and limits, nothing to tune or run.
function labOverview(account = 'options') {
    const { EQUITY_STRATEGIES, LIMITS, LabError, OPTIONS_STRATEGIES } = require('./algo-trader/research-lab');
    if (account !== 'options' && account !== 'equity') throw new LabError('account must be options or equity');
    const ids = account === 'equity' ? EQUITY_STRATEGIES : OPTIONS_STRATEGIES;
    return {
        account,
        limits: { maxUnderlyings: LIMITS.maxUnderlyings, minFreeMemoryMb: LIMITS.minFreeMemoryMb, earliestStart: LIMITS.earliestStart, concurrentJobs: 1 },
        active_job: null,
        start_available: false,
        strategies: ids.map(id => ({ id, account, parameters: {}, tunable: [], profiles: [], history: [] })),
    };
}

module.exports = { labOverview, universePayload, universeHistory, algoPayload };
