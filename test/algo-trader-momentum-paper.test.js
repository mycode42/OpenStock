'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { buildMomentumPayload } = require('../server/algo-trader/momentum-paper');

function fixture(t) {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'momentum-view-'));
    t.after(() => fs.rmSync(root, { recursive: true, force: true }));
    const dir = path.join(root, 'data', 'research', 'momentum-paper');
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, 'state.json'), JSON.stringify({ last_quarter: '2026-Q4', last_rebalance: '2026-10-01' }));
    fs.writeFileSync(path.join(dir, 'decisions.jsonl'), [
        JSON.stringify({ quarter: '2026-Q4', basket: [{ symbol: 'AAA', momentum_6m: 0.5, weight: 0.5, close: 10 }, { symbol: 'BBB', momentum_6m: 0.3, weight: 0.5, close: 20 }] }),
        JSON.stringify({ quarter: '2026-Q4', basket: [] }),
    ].join('\n') + '\n');
    return root;
}

const keys = { ALPACA_MOMENTUM_API_KEY: 'k', ALPACA_MOMENTUM_API_SECRET: 's' };

test('the momentum view reads the account and the rebalance log, and shows the latest basket', async t => {
    const root = fixture(t);
    const fetchImpl = async url => {
        if (url.endsWith('/account')) return { ok: true, json: async () => ({ equity: '100500', cash: '2000', last_equity: '100000' }) };
        return { ok: true, json: async () => [{ symbol: 'AAA', qty: '50', avg_entry_price: '9', current_price: '10', market_value: '500', unrealized_pl: '50', unrealized_plpc: '0.11' }] };
    };
    const payload = await buildMomentumPayload({ root, env: keys, fetchImpl });
    assert.equal(payload.available, true);
    assert.equal(payload.equity, 100500);
    assert.equal(payload.positions[0].symbol, 'AAA');
    assert.deepEqual(payload.basket.map(row => row.symbol), ['AAA', 'BBB']);
    assert.equal(payload.last_rebalance, '2026-10-01');
});

test('without keys or with a failing account the view says so and still shows the book', async t => {
    const root = fixture(t);
    const none = await buildMomentumPayload({ root, env: {}, fetchImpl: async () => { throw new Error('no network'); } });
    assert.equal(none.available, false);
    assert.equal(none.basket.length, 2);
    const failing = await buildMomentumPayload({ root, env: keys, fetchImpl: async () => ({ ok: false, status: 403 }) });
    assert.equal(failing.available, false);
});

test('a non-paper account URL is refused without a live opt-in', async t => {
    const root = fixture(t);
    const payload = await buildMomentumPayload({ root, env: { ...keys, ALPACA_MOMENTUM_BASE_URL: 'https://api.alpaca.markets/v2' }, fetchImpl: async () => { throw new Error('must not be called'); } });
    assert.equal(payload.available, false);
});

test('the momentum view carries equity history, metrics and recent orders in the same shape as the other pages', async t => {
    const root = fixture(t);
    const day = (d, hour = 20) => Math.floor(Date.UTC(2026, 10, d, hour) / 1000);
    const fetchImpl = async url => {
        if (url.endsWith('/account')) return { ok: true, json: async () => ({ equity: '110', cash: '10', last_equity: '100' }) };
        if (url.endsWith('/positions')) return { ok: true, json: async () => [] };
        if (url.includes('/account/portfolio/history')) return { ok: true, json: async () => ({ timestamp: [day(9), day(10), day(11)], equity: [100, 105, 110] }) };
        return { ok: true, json: async () => [{ symbol: 'AAA', side: 'buy', qty: '5', type: 'limit', limit_price: '10', status: 'filled', submitted_at: '2026-11-10T14:00:00Z' }] };
    };
    const payload = await buildMomentumPayload({ root, env: keys, fetchImpl });
    assert.equal(payload.history.length, 3);
    assert.equal(payload.history[2].equity, 110);
    assert.equal(payload.history[1].daily_pnl, 5);
    assert.ok(Math.abs(payload.metrics.total_return_pct - 10) < 1e-9);
    assert.equal(payload.activity[0].kind, 'order');
    assert.match(payload.activity[0].title, /BUY 5 AAA/);
});
