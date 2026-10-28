'use strict';

const STOCKS = Object.freeze([
    { id: -101, symbol: 'AAPL', name: 'Apple Inc.', base: 219.42, buyIn: 205.1, quantity: 8, held: true },
    { id: -102, symbol: 'NVDA', name: 'NVIDIA Corporation', base: 132.86, buyIn: 118.35, quantity: 12, held: true },
    { id: -103, symbol: 'SAP', name: 'SAP SE', base: 246.8, buyIn: 232.4, quantity: 5, held: true },
    { id: -104, symbol: 'AIR', name: 'Airbus SE', base: 172.64, buyIn: 176.2, quantity: 0, held: false },
    { id: -105, symbol: 'MC', name: 'LVMH Moët Hennessy Louis Vuitton', base: 582.3, buyIn: 0, quantity: 0, held: false },
    { id: -106, symbol: 'ASML', name: 'ASML Holding N.V.', base: 724.6, buyIn: 690.2, quantity: 2, held: true },
]);

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

function dashboardStocks(nowValue = new Date()) {
    const now = date(nowValue);
    const updatedAt = now.toISOString();
    return STOCKS.map(stock => {
        const price = demoPrice(stock.base, stock.symbol, now);
        const spread = price * 0.0004;
        const buy = Number((price - spread / 2).toFixed(4));
        const sell = Number((price + spread / 2).toFixed(4));
        const activeLevels = [
            { id: stock.id * -10, side: 'BUY', price_eur: Number((price * 0.94).toFixed(2)) },
            { id: stock.id * -10 - 1, side: 'SELL', price_eur: Number((price * 1.08).toFixed(2)) },
        ];
        return {
            id: stock.id,
            symbol: stock.symbol,
            name: stock.name,
            currency: 'EUR',
            asset_type: 'stock',
            enabled: 1,
            archived: 0,
            current_price_eur: price,
            current_bid_eur: buy,
            current_ask_eur: sell,
            last_price_update: updatedAt,
            created_at: updatedAt,
            updated_at: updatedAt,
            broker_mapping: null,
            broker_quote: { bid_eur: buy, ask_eur: sell, spread_eur: Number((sell - buy).toFixed(4)), quote_time: updatedAt, observed_at: updatedAt, source: 'demo' },
            market_data: { source: 'demo', label: 'Simulated market data', fallback: false, quote_time: updatedAt },
            position: {
                owned: stock.held,
                quantity: stock.quantity,
                buy_in_price_eur: stock.held ? stock.buyIn : null,
                source: 'demo portfolio',
            },
            alert_levels: activeLevels.map(level => ({ ...level, enabled: 1, active: 1, triggered: 0, retired: 0, trigger_mode: 'PRICE' })),
        };
    });
}

function livePrices(nowValue = new Date()) {
    const now = date(nowValue);
    return {
        observed_at: now.toISOString(),
        refresh_ms: 1000,
        cached: false,
        refreshing: false,
        prices: dashboardStocks(now).map(stock => ({
            stock_id: stock.id,
            price_eur: stock.current_price_eur,
            bid_eur: stock.broker_quote.bid_eur,
            ask_eur: stock.broker_quote.ask_eur,
            quote_time: now.toISOString(),
            observed_at: now.toISOString(),
            source: 'demo',
            fallback: false,
        })),
    };
}

function stockHistory(stockId, range, nowValue = new Date()) {
    const stock = STOCKS.find(item => item.id === Number(stockId));
    if (!stock) return null;
    const now = date(nowValue);
    const points = series(demoPrice(stock.base, stock.symbol, now), stock.symbol, range, now);
    if (!points) return null;
    return {
        stock_id: stock.id,
        symbol: stock.symbol,
        range,
        currency: 'EUR',
        native_eur: true,
        source: 'demo',
        bucket_seconds: range === '1d' ? 900 : 3600,
        max_gap_seconds: 0,
        start_at: points[0].recorded_at,
        end_at: points[points.length - 1].recorded_at,
        points,
    };
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

function equityHistory(account, now) {
    const days = 75;
    const start = account === 'options' ? 38_000 : 100_000;
    const end = now.getTime();
    const daily = Array.from({ length: days }, (_, index) => {
        const progress = index / (days - 1);
        const wave = Math.sin(progress * Math.PI * 12) * 0.012 + Math.sin(progress * Math.PI * 31) * 0.004;
        const equity = start * (1 + progress * 0.071 + wave);
        const timestamp = new Date(end - (days - index) * 86_400_000).toISOString();
        return {
            date: timestamp.slice(0, 10), timestamp,
            equity: Number(equity.toFixed(2)),
            cash: Number((equity * 0.31).toFixed(2)),
            invested: Number((equity * 0.69).toFixed(2)),
            position_count: account === 'options' ? 3 : 5,
        };
    });
    const latestDailyEquity = daily[daily.length - 1].equity;
    const intraday = Array.from({ length: 9 }, (_, index) => {
        const progress = index / 8;
        const equity = latestDailyEquity * (1 + Math.sin(progress * Math.PI * 4) * 0.0025);
        const timestamp = new Date(end - (8 - index) * 2 * 60 * 60_000).toISOString();
        return {
            date: timestamp.slice(0, 10), timestamp,
            equity: Number(equity.toFixed(2)),
            cash: Number((equity * 0.31).toFixed(2)),
            invested: Number((equity * 0.69).toFixed(2)),
            position_count: account === 'options' ? 3 : 5,
        };
    });
    return [...daily, ...intraday];
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

module.exports = { dashboardStocks, livePrices, stockHistory, universePayload, universeHistory, algoPayload };
