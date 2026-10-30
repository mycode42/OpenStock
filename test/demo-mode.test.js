const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const Database = require('better-sqlite3');
const { initDatabase } = require('../database/init');
const { seedDemoWatchlist } = require('../server/demo-seed');
const { createDemoBroker } = require('../server/demo-broker');
const { AlpacaError } = require('../lib/brokers/alpaca');
const { cancelOpenOrder, placeLevelOrder, syncOpenOrders } = require('../lib/alpaca-orders');

const NOW = new Date('2026-10-30T10:00:00Z');

function makeDb(t) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-demo-mode-'));
    const file = path.join(dir, 'demo.db');
    initDatabase({ dbPath: file, seedDefaults: false, silent: true });
    t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
    const db = new Database(file);
    t.after(() => db.close());
    return db;
}

test('the demo seed creates the watchlist, its levels and a retired level once', t => {
    const db = makeDb(t);
    assert.equal(seedDemoWatchlist(db, NOW), true);
    assert.equal(seedDemoWatchlist(db, NOW), false);
    assert.equal(db.prepare('SELECT COUNT(*) AS count FROM alpaca_watchlist').get().count, 6);
    assert.equal(db.prepare('SELECT COUNT(*) AS count FROM alpaca_alert_levels WHERE retired = 1').get().count, 1);
    const autoOrder = db.prepare('SELECT order_budget_usd AS budget_usd FROM alpaca_alert_levels WHERE auto_order = 1').get();
    assert.equal(autoOrder.budget_usd, 500);
});

test('the simulated broker never accepts orders outside the demo database', async () => {
    const broker = createDemoBroker({ now: () => NOW, ordersEnabled: false });
    await assert.rejects(
        broker.submitOrder({ symbol: 'AAPL', qty: '1', side: 'buy', type: 'limit', limit_price: '200', client_order_id: 'x' }),
        error => error instanceof AlpacaError && error.status === 409,
    );
});

test('a marketable demo order fills on the next status check and updates the position', async t => {
    const db = makeDb(t);
    seedDemoWatchlist(db, NOW);
    const broker = createDemoBroker({ now: () => NOW, ordersEnabled: true });
    const tesla = db.prepare(`SELECT id FROM alpaca_alert_levels WHERE price_usd = 396`).get();
    const { order } = await placeLevelOrder({ db, client: broker, levelId: tesla.id, amountUsd: 1000, now: () => NOW });
    assert.equal(order.status, 'NEW');
    await syncOpenOrders({ db, client: broker, now: () => NOW });
    const filled = db.prepare('SELECT status, filled_qty, filled_avg_price_usd FROM alpaca_orders WHERE id = ?').get(order.id);
    assert.deepEqual({ ...filled }, { status: 'FILLED', filled_qty: 2, filled_avg_price_usd: 396 });
    const positions = await broker.getPositions();
    assert.equal(positions.find(row => row.symbol === 'TSLA').qty, '2');
});

test('a resting demo order can be cancelled and then reads as cancelled', async t => {
    const db = makeDb(t);
    seedDemoWatchlist(db, NOW);
    const broker = createDemoBroker({ now: () => NOW, ordersEnabled: true });
    const level = db.prepare('SELECT id FROM alpaca_alert_levels WHERE price_usd = 124').get();
    const { order } = await placeLevelOrder({ db, client: broker, levelId: level.id, amountUsd: 500, now: () => NOW });
    await cancelOpenOrder({ db, client: broker, orderId: order.id });
    await syncOpenOrders({ db, client: broker, now: () => NOW });
    assert.equal(db.prepare('SELECT status FROM alpaca_orders WHERE id = ?').get(order.id).status, 'CANCELED');
});

test('demo quotes are simulated and labelled as demo data', async () => {
    const broker = createDemoBroker({ now: () => NOW, ordersEnabled: false });
    const payload = await broker.getLatestQuotes(['aapl', 'UNKNOWN']);
    assert.equal(payload.feed, 'simulated');
    assert.ok(payload.quotes.AAPL.bp < payload.quotes.AAPL.ap);
    assert.equal(payload.quotes.UNKNOWN, undefined);
});
