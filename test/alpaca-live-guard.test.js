const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const Database = require('better-sqlite3');
const { initDatabase } = require('../database/init');
const orders = require('../lib/alpaca-orders');

const LIVE = { ALPACA_MANUAL_BASE_URL: 'https://api.alpaca.markets/v2' };

function makeDb(t) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-live-guard-'));
    const file = path.join(dir, 'test.db');
    initDatabase({ dbPath: file, seedDefaults: false, silent: true });
    t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
    const db = new Database(file);
    db.pragma('foreign_keys = OFF');
    t.after(() => db.close());
    return db;
}

function seedLevel(db, { side = 'BUY', price = 100 } = {}) {
    const watchlistId = Number(db.prepare(`INSERT INTO alpaca_watchlist (symbol, name, source_stock_id, copied_fx_rate)
        VALUES ('AAPL', 'Apple', ?, 1)`).run(Math.floor(Math.random() * 1e9)).lastInsertRowid);
    return Number(db.prepare(`INSERT INTO alpaca_alert_levels (watchlist_id, source_alert_level_id, side, price_usd)
        VALUES (?, ?, ?, ?)`).run(watchlistId, Math.floor(Math.random() * 1e9), side, price).lastInsertRowid);
}

function fakeClient(position = null) {
    return { async getPositions() { return position ? [position] : []; } };
}

test('a live account refuses orders until ALPACA_ALLOW_LIVE=1 is set', async t => {
    const db = makeDb(t);
    const levelId = seedLevel(db);
    await assert.rejects(
        orders.placeLevelOrder({ db, client: fakeClient(), levelId, env: LIVE, dryRun: true, amountUsd: 250 }),
        error => error.status === 403 && /ALPACA_ALLOW_LIVE=1/.test(error.message),
    );
    const plan = await orders.placeLevelOrder({ db, client: fakeClient(), levelId, env: { ...LIVE, ALPACA_ALLOW_LIVE: '1' }, dryRun: true, amountUsd: 250 });
    assert.equal(plan.plan.qty, 2);
});

test('an automatic live order is refused without the opt-in too', async t => {
    const db = makeDb(t);
    const levelId = seedLevel(db);
    db.prepare('UPDATE alpaca_alert_levels SET auto_order=1, order_budget_usd=250 WHERE id=?').run(levelId);
    await assert.rejects(orders.placeTriggeredOrder({ db, client: fakeClient(), levelId, env: LIVE }), error => error.status === 403);
});

test('an order at exactly the $100,000 cap is allowed and one dollar over is refused', async t => {
    const db = makeDb(t);
    const levelId = seedLevel(db, { price: 100 });
    const env = { ...LIVE, ALPACA_ALLOW_LIVE: '1' };
    const atCap = await orders.placeLevelOrder({ db, client: fakeClient(), levelId, env, dryRun: true, amountUsd: 100000 });
    assert.equal(atCap.plan.notional_usd, 100000);
    await assert.rejects(
        orders.placeLevelOrder({ db, client: fakeClient(), levelId, env, dryRun: true, amountUsd: 100100 }),
        error => error.status === 400 && /per-order limit/.test(error.message),
    );
});

test('a SELL of a position larger than the cap is refused rather than split', async t => {
    const db = makeDb(t);
    const levelId = seedLevel(db, { side: 'SELL', price: 100 });
    const position = { symbol: 'AAPL', qty: '1500', avg_entry_price: '50' };
    await assert.rejects(
        orders.placeLevelOrder({ db, client: fakeClient(position), levelId, env: { ...LIVE, ALPACA_ALLOW_LIVE: '1' }, dryRun: true }),
        error => error.status === 400 && /per-order limit/.test(error.message),
    );
});

test('settings report the live state and the cap', () => {
    assert.equal(orders.publicSettings({ ...LIVE }).live_allowed, false);
    const settings = orders.publicSettings({ ...LIVE, ALPACA_ALLOW_LIVE: '1' });
    assert.equal(settings.live_allowed, true);
    assert.equal(settings.max_order_usd, 100000);
    assert.equal(settings.mode, 'live');
});
