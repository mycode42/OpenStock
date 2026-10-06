'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { buildCashFlowEvents, buildReplaySnapshots } = require('../lib/equity-replay');

test('equity replay combines manual basis with confirmed broker sale without double counting the matched buy', () => {
    const events = buildCashFlowEvents({
        capitalEvents: [{ event_type: 'INITIAL', amount_eur: 1000, occurred_at: '2026-01-01 00:00:00' }],
        trades: [{ id: 1, trade_id: 'OST-manual-lot', stock_id: 1, opened_at: '2026-01-02 10:00:00', closed_at: '2026-01-03 10:00:00', quantity: 5, entry_price_eur: 10, entry_fee_eur: 1, exit_price_eur: 12, exit_fee_eur: 1, dividend_eur: 0, notes: 'Trade Republic confirmed execution' }],
        brokerExecutions: [
            { execution_identity: 'abcdef0123456789abcd001', stock_id: 1, side: 'buy', quantity: 5, execution_price_eur: 10, fees_eur: 1, executed_at: '2026-01-02T10:00:00.000Z' },
            { execution_identity: 'fedcba0123456789fedc001', stock_id: 1, side: 'sell', quantity: 5, execution_price_eur: 12, fees_eur: 1, executed_at: '2026-01-03T10:00:00.000Z' }
        ],
        mergeEvents: [{ target_trade_id: 1, source_trade_id: 'source-buy', source_trade_json: JSON.stringify({ trade_id: 'TR-EXEC-abcdef0123456789abcd', quantity: 5, entry_price_eur: 10, entry_fee_eur: 1 }) }]
    });
    assert.equal(events.reduce((sum, event) => sum + event.delta, 0), 1008);
    assert.equal(events.filter(event => event.kind === 'broker_buy').length, 0);
});

test('equity replay preserves dividends when broker fills match Journal trades', () => {
    const events = buildCashFlowEvents({
        trades: [{ id: 1, trade_id: 'TR-EXEC-buy', stock_id: 1, opened_at: '2026-01-01 10:00:00', closed_at: '2026-01-02 10:00:00', quantity: 2, entry_price_eur: 10, entry_fee_eur: 1, exit_price_eur: 12, exit_fee_eur: 1, dividend_eur: 4.5 }],
        brokerExecutions: [
            { execution_identity: 'buy-identity', stock_id: 1, side: 'buy', quantity: 2, execution_price_eur: 10, fees_eur: 1, executed_at: '2026-01-01T10:00:00.000Z' },
            { execution_identity: 'sell-identity', stock_id: 1, side: 'sell', quantity: 2, execution_price_eur: 12, fees_eur: 1, executed_at: '2026-01-02T10:00:00.000Z' }
        ]
    });
    assert.equal(events.reduce((sum, event) => sum + event.delta, 0), 6.5);
    assert.equal(events.filter(event => event.kind === 'trade_dividend').length, 1);
});

test('equity replay prices daily holdings from historical marks and matches the current mark', () => {
    const events = [
        { at: Date.parse('2026-01-01T00:00:00Z'), stock_id: null, quantity: 0, delta: 1000, kind: 'capital_INITIAL' },
        { at: Date.parse('2026-01-02T10:00:00Z'), stock_id: 1, quantity: 10, delta: -101, kind: 'broker_buy' },
        { at: Date.parse('2026-01-03T10:00:00Z'), stock_id: 1, quantity: -4, delta: 47, kind: 'broker_sell' }
    ];
    const points = buildReplaySnapshots({
        events,
        priceHistory: [
            { stock_id: 1, quote_time: '2026-01-02T00:00:00Z', price_eur: 10 },
            { stock_id: 1, quote_time: '2026-01-03T00:00:00Z', price_eur: 12 }
        ],
        stocks: [{ id: 1, symbol: 'ONE', current_price_eur: 13 }],
        startAt: '2026-01-01 00:00:00', now: new Date('2026-01-04T12:00:00Z')
    });
    assert.equal(points.length, 4);
    assert.deepEqual(points.map(point => Number(point.equity_eur.toFixed(2))), [1000, 999, 1018, 1024]);
    assert.equal(points.at(-1).missing_prices.length, 0);
});
