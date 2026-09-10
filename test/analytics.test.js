const test = require('node:test');
const assert = require('node:assert/strict');
const { calculateTrade, calculateDashboard } = require('../lib/analytics');

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
