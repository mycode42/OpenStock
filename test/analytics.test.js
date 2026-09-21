const test = require('node:test');
const assert = require('node:assert/strict');
const { calculateTrade, calculateDashboard, scaleEquityCurve, snapshotBucketAt } = require('../lib/analytics');

test('closed trade includes both fees in net P/L and return', () => {
    const trade = calculateTrade({ quantity: 10, entry_price_eur: 100, entry_fee_eur: 5, exit_price_eur: 120, exit_fee_eur: 5, opened_at: '2026-01-01 00:00:00', closed_at: '2026-01-11 00:00:00' });
    assert.equal(trade.net_pnl_eur, 190);
    assert.equal(trade.return_pct, 190 / 1005 * 100);
    assert.equal(trade.holding_days, 10);
});

test('received dividends are included in closed-trade P/L and cash', () => {
    const result = calculateDashboard({
        stocks: [{ id: 1, current_price_eur: 100 }],
        capitalEvents: [{ event_type: 'INITIAL', amount_eur: 1000, occurred_at: '2026-01-01 00:00:00' }],
        trades: [{ stock_id: 1, quantity: 5, entry_price_eur: 100, entry_fee_eur: 2, exit_price_eur: 110, exit_fee_eur: 3, dividend_eur: 12, opened_at: '2026-01-02 00:00:00', closed_at: '2026-01-04 00:00:00' }]
    });
    assert.equal(result.realized.net_eur, 57);
    assert.equal(result.realized.dividends_eur, 12);
    assert.equal(result.capital.available_cash_eur, 1057);
});

test('capital events do not affect realized trading P/L', () => {
    const result = calculateDashboard({
        stocks: [{ id: 1, current_price_eur: 130 }],
        capitalEvents: [{ event_type: 'INITIAL', amount_eur: 1000, occurred_at: '2026-01-01 00:00:00' }, { event_type: 'DEPOSIT', amount_eur: 500, occurred_at: '2026-01-02 00:00:00' }],
        trades: [{ stock_id: 1, quantity: 5, entry_price_eur: 100, entry_fee_eur: 2, exit_price_eur: 110, exit_fee_eur: 3, opened_at: '2026-01-03 00:00:00', closed_at: '2026-01-04 00:00:00' }],
        now: '2026-02-10T00:00:00Z'
    });
    assert.equal(result.realized.net_eur, 45);
    assert.equal(result.capital.available_cash_eur, 1545);
    assert.equal(result.capital.current_eur, 1545);
});

test('open positions stay out of completed statistics and use current value only for unrealized P/L', () => {
    const result = calculateDashboard({
        stocks: [{ id: 1, current_price_eur: 110 }],
        capitalEvents: [{ event_type: 'INITIAL', amount_eur: 2000, occurred_at: '2026-01-01 00:00:00' }],
        trades: [{ stock_id: 1, quantity: 10, entry_price_eur: 100, entry_fee_eur: 5, opened_at: '2026-01-02 00:00:00' }]
    });
    assert.equal(result.trades.completed, 0);
    assert.equal(result.trades.open, 1);
    assert.equal(result.realized.net_eur, 0);
    assert.equal(result.capital.unrealized_pnl_eur, 95);
    assert.equal(result.capital.current_eur, 2095);
});

test('equity curve scaling chooses readable hourly samples for several days of minute data', () => {
    const start = Date.parse('2026-01-01T00:00:00Z');
    const points = Array.from({ length: 4 * 24 * 60 + 1 }, (_, index) => ({ at: new Date(start + index * 60000).toISOString(), equity_eur: 100 + index / 100 }));
    const scaled = scaleEquityCurve(points);
    assert.equal(scaled.resolution, 'hourly');
    assert.ok(scaled.points.length >= 96 && scaled.points.length <= 98);
    assert.equal(scaled.points[0], points[0]);
    assert.equal(scaled.points.at(-1), points.at(-1));
});

test('equity snapshots share a stable fifteen-minute bucket', () => {
    assert.equal(snapshotBucketAt(new Date('2026-01-01T12:01:00Z')), '2026-01-01 12:00:00');
    assert.equal(snapshotBucketAt(new Date('2026-01-01T12:14:59Z')), '2026-01-01 12:00:00');
    assert.equal(snapshotBucketAt(new Date('2026-01-01T12:15:00Z')), '2026-01-01 12:15:00');
});

test('minute snapshots are not misrepresented as daily volatility observations', () => {
    const snapshots = Array.from({ length: 60 }, (_, index) => ({ snapshot_at: `2026-01-01 12:${String(index).padStart(2, '0')}:00`, equity_eur: 100 + index, cash_eur: 100 + index, position_value_eur: 0, net_contributions_eur: 100 }));
    const result = calculateDashboard({ snapshots });
    assert.equal(result.risk.volatility_pct, null);
    assert.equal(result.risk.sharpe_ratio, null);
});

test('drawdown is calculated from snapshots and volatility needs sufficient data', () => {
    const result = calculateDashboard({ snapshots: [
        { snapshot_at: '2026-01-01 00:00:00', equity_eur: 100, cash_eur: 100, position_value_eur: 0, net_contributions_eur: 100 },
        { snapshot_at: '2026-01-02 00:00:00', equity_eur: 120, cash_eur: 120, position_value_eur: 0, net_contributions_eur: 100 },
        { snapshot_at: '2026-01-03 00:00:00', equity_eur: 90, cash_eur: 90, position_value_eur: 0, net_contributions_eur: 100 }
    ] });
    assert.equal(result.risk.max_drawdown_pct, -25);
    assert.equal(result.risk.current_drawdown_pct, -25);
    assert.equal(result.risk.volatility_pct, null);
    assert.equal(result.risk.sharpe_ratio, null);
});

test('capital deposits do not create a false equity drawdown', () => {
    const result = calculateDashboard({ snapshots: [
        { snapshot_at: '2026-01-01 00:00:00', equity_eur: 100, cash_eur: 100, position_value_eur: 0, net_contributions_eur: 100 },
        { snapshot_at: '2026-01-02 00:00:00', equity_eur: 100, cash_eur: 100, position_value_eur: 0, net_contributions_eur: 100 },
        { snapshot_at: '2026-01-03 00:00:00', equity_eur: 200, cash_eur: 200, position_value_eur: 0, net_contributions_eur: 200 }
    ] });
    assert.ok(Math.abs(result.risk.max_drawdown_pct) < 0.001);
    assert.ok(Math.abs(result.risk.current_drawdown_pct) < 0.001);
});

test('daily performance aggregates completed trades by close date', () => {
    const result = calculateDashboard({ trades: [
        { stock_id: 1, symbol: 'ONE', quantity: 1, entry_price_eur: 10, entry_fee_eur: 0, exit_price_eur: 12, exit_fee_eur: 0, opened_at: '2026-01-01 09:00:00', closed_at: '2026-01-02 09:00:00' },
        { stock_id: 1, symbol: 'ONE', quantity: 1, entry_price_eur: 10, entry_fee_eur: 0, exit_price_eur: 9, exit_fee_eur: 0, opened_at: '2026-01-01 10:00:00', closed_at: '2026-01-02 12:00:00' },
        { stock_id: 1, symbol: 'ONE', quantity: 1, entry_price_eur: 10, entry_fee_eur: 0, exit_price_eur: 15, exit_fee_eur: 0, opened_at: '2026-01-02 10:00:00', closed_at: '2026-01-03 09:00:00' }
    ] });
    assert.deepEqual(result.daily_performance, [
        { day: '2026-01-02', pnl_eur: 1, trades: 2, wins: 1 },
        { day: '2026-01-03', pnl_eur: 5, trades: 1, wins: 1 }
    ]);
    assert.equal(result.performance.average_daily_pnl_eur, 3);
    assert.equal(result.performance.average_monthly_pnl_eur, 6);
    assert.equal(result.performance.average_daily_return_pct, null);
});

test('compounding metrics are derived from current capital and the observed return', () => {
    const result = calculateDashboard({
        capitalEvents: [{ event_type: 'INITIAL', amount_eur: 1000, occurred_at: '2026-01-01 00:00:00' }],
        stocks: [{ id: 1, current_price_eur: 110 }],
        trades: [{ stock_id: 1, quantity: 10, entry_price_eur: 100, entry_fee_eur: 0, opened_at: '2026-01-02 00:00:00' }],
        now: '2026-02-01T00:00:00Z'
    });
    assert.equal(result.performance.investment_profit_eur, 100);
    assert.equal(result.performance.growth_multiple, 1.1);
    assert.ok(Number.isFinite(result.performance.annualized_return_pct));
    assert.ok(Number.isFinite(result.performance.monthly_compounding_rate_pct));
    assert.ok(result.performance.projected_one_year_capital_eur > result.capital.current_eur);
});
