const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const Database = require('better-sqlite3');
const { initDatabase } = require('../database/init');
const orders = require('../lib/alpaca-orders');
const { AlpacaError } = require('../lib/brokers/alpaca');

const ENABLED = { };

function makeDb(t) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-orders-'));
    const file = path.join(dir, 'test.db');
    initDatabase({ dbPath: file, seedDefaults: false, silent: true });
    t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
    const db = new Database(file);
    db.pragma('foreign_keys = OFF');
    t.after(() => db.close());
    return db;
}

function seedLevel(db, { symbol = 'AAPL', side = 'BUY', price = 100, enabled = 1, triggered = 0, retired = 0 } = {}) {
    let watchlist = db.prepare('SELECT id FROM alpaca_watchlist WHERE symbol=?').get(symbol);
    if (!watchlist) {
        const id = db.prepare(`INSERT INTO alpaca_watchlist (symbol, name, source_stock_id, copied_fx_rate)
            VALUES (?, ?, ?, 1)`).run(symbol, symbol, Math.floor(Math.random() * 1e9)).lastInsertRowid;
        watchlist = { id };
    }
    const watchlistId = Number(watchlist.id);
    return Number(db.prepare(`INSERT INTO alpaca_alert_levels (watchlist_id, source_alert_level_id, side, price_usd, enabled, active, triggered, retired)
        VALUES (?, ?, ?, ?, ?, 1, ?, ?)`).run(watchlistId, Math.floor(Math.random() * 1e9), side, price, enabled, triggered, retired).lastInsertRowid);
}

function fakeClient({ bid = 99, ask = 101, position = null, submitError = null, submitStatus = 'new' } = {}) {
    const calls = { submitted: [], cancelled: [] };
    return {
        calls,
        async getLatestQuotes(symbols) {
            return { feed: 'iex', quotes: Object.fromEntries(symbols.map(s => [s, { bp: bid, ap: ask, t: '2026-10-29T10:00:00Z' }])) };
        },
        async getPositions() { return position ? [position] : []; },
        async submitOrder(body) {
            if (submitError) throw submitError;
            calls.submitted.push(body);
            return { id: `broker-${calls.submitted.length}`, status: submitStatus };
        },
        async cancelOrder(id) { calls.cancelled.push(id); return null; },
        async getOrder(id) { return { id, status: 'filled', filled_qty: '2', filled_avg_price: '99.5' }; },
        async getOrderByClientId() { throw new AlpacaError('not found', 404); },
    };
}

test('buy level sizes whole shares from the notional and places a day limit', async t => {
    const db = makeDb(t);
    const levelId = seedLevel(db, { side: 'BUY', price: 100 });
    const client = fakeClient();
    const result = await orders.placeLevelOrder({ db, client, levelId, env: ENABLED });
    assert.equal(result.order.qty, 2);
    assert.equal(result.order.status, 'NEW');
    assert.equal(result.order.broker_order_id, 'broker-1');
    assert.deepEqual(client.calls.submitted[0], {
        symbol: 'AAPL', qty: '2', side: 'buy', type: 'limit', time_in_force: 'day',
        limit_price: '100.00', client_order_id: result.order.client_order_id, extended_hours: true,
    });
});

test('dry run returns the plan and submits nothing', async t => {
    const db = makeDb(t);
    const levelId = seedLevel(db, { side: 'BUY', price: 100 });
    const client = fakeClient();
    const { plan } = await orders.placeLevelOrder({ db, client, levelId, env: ENABLED, dryRun: true });
    assert.equal(plan.qty, 2);
    assert.equal(plan.mode, 'paper');
    assert.equal(client.calls.submitted.length, 0);
    assert.equal(db.prepare('SELECT COUNT(*) AS n FROM alpaca_orders').get().n, 0);
});

test('a marketable buy limit is allowed and never fills above its price', async t => {
    const db = makeDb(t);
    const levelId = seedLevel(db, { side: 'BUY', price: 101 });
    const client = fakeClient({ ask: 100 });
    const result = await orders.placeLevelOrder({ db, client, levelId, env: ENABLED });
    assert.equal(result.order.status, 'NEW');
    assert.equal(client.calls.submitted[0].limit_price, '101.00');
});

test('amount sizes the buy in whole shares, with no per-order or daily cap', async t => {
    const db = makeDb(t);
    const levelId = seedLevel(db, { side: 'BUY', price: 100 });
    const client = fakeClient();
    const { plan } = await orders.placeLevelOrder({ db, client, levelId, env: ENABLED, dryRun: true, amountUsd: 100000 });
    assert.equal(plan.qty, 1000);
    assert.equal(plan.notional_usd, 100000);
    await assert.rejects(orders.placeLevelOrder({ db, client, levelId, env: ENABLED, dryRun: true, amountUsd: 50 }), err => err.status === 400);
});

test('sell limit exits the full position and refuses without one', async t => {
    const db = makeDb(t);
    const levelId = seedLevel(db, { side: 'SELL', price: 110 });
    await assert.rejects(orders.placeLevelOrder({ db, client: fakeClient(), levelId, env: ENABLED }), err => err.status === 409);
    const client = fakeClient({ position: { symbol: 'AAPL', qty: '7' } });
    const result = await orders.placeLevelOrder({ db, client, levelId, env: ENABLED });
    assert.equal(result.order.qty, 7);
    assert.equal(client.calls.submitted[0].side, 'sell');
});

test('one live order per level, and triggered levels take no orders', async t => {
    const db = makeDb(t);
    const levelId = seedLevel(db, { side: 'BUY', price: 100 });
    await orders.placeLevelOrder({ db, client: fakeClient(), levelId, env: ENABLED });
    await assert.rejects(orders.placeLevelOrder({ db, client: fakeClient(), levelId, env: ENABLED }), err => err.status === 409);
    const triggeredId = seedLevel(db, { side: 'BUY', price: 100, triggered: 1, retired: 1 });
    await assert.rejects(orders.placeLevelOrder({ db, client: fakeClient(), levelId: triggeredId, env: ENABLED }), err => err.status === 409);
});

test('a rejected submit is recorded as REJECTED and frees the level', async t => {
    const db = makeDb(t);
    const levelId = seedLevel(db, { side: 'BUY', price: 100 });
    const rejected = new AlpacaError('Alpaca API request failed (422): insufficient buying power', 422);
    const result = await orders.placeLevelOrder({ db, client: fakeClient({ submitError: rejected }), levelId, env: ENABLED });
    assert.equal(result.order.status, 'REJECTED');
    await orders.placeLevelOrder({ db, client: fakeClient(), levelId, env: ENABLED });
});

test('sync moves a live order to FILLED and records the fill', async t => {
    const db = makeDb(t);
    const levelId = seedLevel(db, { side: 'BUY', price: 100 });
    const { order } = await orders.placeLevelOrder({ db, client: fakeClient(), levelId, env: ENABLED });
    const summary = await orders.syncOpenOrders({ db, client: fakeClient() });
    assert.equal(summary.updated, 1);
    const row = db.prepare('SELECT * FROM alpaca_orders WHERE id=?').get(order.id);
    assert.equal(row.status, 'FILLED');
    assert.equal(row.filled_qty, 2);
    assert.equal(row.filled_avg_price_usd, 99.5);
});

test('cancel sends DELETE for a confirmed order and marks it pending cancel', async t => {
    const db = makeDb(t);
    const levelId = seedLevel(db, { side: 'BUY', price: 100 });
    const client = fakeClient();
    const { order } = await orders.placeLevelOrder({ db, client, levelId, env: ENABLED });
    const result = await orders.cancelOpenOrder({ db, client, orderId: order.id });
    assert.deepEqual(client.calls.cancelled, ['broker-1']);
    assert.equal(result.order.status, 'PENDING_CANCEL');
});

test('trading is paper unless the live URL is set explicitly', () => {
    assert.equal(orders.tradingMode({}), 'paper');
    assert.equal(orders.tradingMode({ ALPACA_MANUAL_BASE_URL: 'https://paper-api.alpaca.markets/v2' }), 'paper');
    assert.equal(orders.tradingMode({ ALPACA_MANUAL_BASE_URL: 'https://api.alpaca.markets' }), 'live');
});

test('automatic orders need the level opt-in', async t => {
    const db = makeDb(t);
    const levelId = seedLevel(db, { side: 'BUY', price: 100, triggered: 1, retired: 1, enabled: 0 });
    const client = fakeClient();
    const notOptedIn = await orders.placeTriggeredOrder({ db, client, levelId, env: ENABLED });
    assert.match(notOptedIn.skipped, /not set to place real orders/);
    assert.equal(client.calls.submitted.length, 0);
});

test('an opted-in level places its order after it has retired itself', async t => {
    const db = makeDb(t);
    const levelId = seedLevel(db, { side: 'BUY', price: 100, triggered: 1, retired: 1, enabled: 0 });
    db.prepare('UPDATE alpaca_alert_levels SET auto_order=1, order_budget_usd=250 WHERE id=?').run(levelId);
    const client = fakeClient();
    const result = await orders.placeTriggeredOrder({ db, client, levelId, env: ENABLED });
    assert.equal(result.order.status, 'NEW');
    assert.equal(result.order.qty, 2);
    assert.equal(client.calls.submitted.length, 1);
});
