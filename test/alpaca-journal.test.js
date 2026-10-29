const test = require('node:test');
const assert = require('node:assert/strict');
const { buildJournalAnalytics } = require('../lib/alpaca-journal');

const fill = (id, side, qty, price, time) => ({ id, symbol: 'SPCX', side, qty: String(qty), price: String(price), transaction_time: time });

test('a round trip closes when sold back to flat and reports its P/L on the sold quantity', () => {
    const analytics = buildJournalAnalytics({
        account: { equity: 1100, cash: 500 },
        fills: [
            fill('1', 'buy', 10, 100, '2026-01-02T15:00:00Z'),
            fill('2', 'sell', 4, 110, '2026-01-03T15:00:00Z'),
            fill('3', 'sell', 6, 120, '2026-01-05T15:00:00Z'),
        ],
    });
    const [trade] = analytics.trade_list;
    assert.equal(trade.is_closed, true);
    assert.equal(trade.net_pnl_usd, 4 * 110 + 6 * 120 - 10 * 100);
    assert.equal(trade.entry_price_usd, 100);
    assert.equal(trade.group_details.partial_sales.length, 1, 'the final sale is not a partial sale');
    assert.equal(analytics.realized.net_usd, 160);
    assert.equal(analytics.trades.win_rate_pct, 100);
    assert.equal(analytics.daily_performance[0].day, '2026-01-05');
});

test('an open trade is listed as open with its remaining quantity and no P/L yet', () => {
    const analytics = buildJournalAnalytics({
        account: { equity: 1000, cash: 0 },
        fills: [fill('1', 'buy', 10, 100, '2026-01-02T15:00:00Z'), fill('2', 'sell', 3, 105, '2026-01-03T15:00:00Z')],
    });
    const [trade] = analytics.trade_list;
    assert.equal(trade.is_closed, false);
    assert.equal(trade.quantity, 7);
    assert.equal(trade.net_pnl_usd, null);
    assert.deepEqual(analytics.trades, { total: 1, open: 1, closed: 0, win_rate_pct: null });
});

test('sells without an open trade are skipped; deposits and withdrawals feed contributions and events', () => {
    const analytics = buildJournalAnalytics({
        account: { equity: 1200, cash: 300 },
        fills: [fill('1', 'sell', 5, 90, '2026-01-02T15:00:00Z')],
        activities: [
            { activity_type: 'CSD', net_amount: '1000', date: '2026-01-01' },
            { activity_type: 'CSW', net_amount: '-200', date: '2026-02-01' },
        ],
    });
    assert.equal(analytics.trade_list.length, 0);
    assert.equal(analytics.capital.net_contributions_usd, 800);
    assert.equal(analytics.capital.current_return_pct, 50);
    assert.deepEqual(analytics.capital_events.map(event => event.event_type), ['WITHDRAWAL', 'DEPOSIT']);
    assert.equal(analytics.capital_events[1].amount_usd, 1000);
});

test('equity curve uses only finite portfolio history points', () => {
    const analytics = buildJournalAnalytics({
        account: { equity: 1000, cash: 0 },
        history: { timestamp: [1767225600, 1767312000, 1767398400], equity: [1000, null, 1010] },
    });
    assert.equal(analytics.equity_available, true);
    assert.deepEqual(analytics.equity_curve.map(point => point.equity_usd), [1000, 1010]);
});

test('equity curve starts at the first funded point, not at the zeros before funding', () => {
    const analytics = buildJournalAnalytics({
        account: { equity: 1010, cash: 0 },
        history: { timestamp: [1767225600, 1767312000, 1767398400, 1767484800], equity: [0, 0, 500, 1010] },
    });
    assert.deepEqual(analytics.equity_curve.map(point => point.equity_usd), [500, 1010]);
});
