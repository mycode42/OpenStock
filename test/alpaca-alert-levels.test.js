const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const Database = require('better-sqlite3');
const { initDatabase } = require('../database/init');
const levels = require('../lib/alpaca-alert-levels');

function makeDb(t) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-alpaca-levels-'));
    const file = path.join(dir, 'test.db');
    initDatabase({ dbPath: file, seedDefaults: false, silent: true });
    t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
    const db = new Database(file);
    db.pragma('foreign_keys = OFF');
    t.after(() => db.close());
    db.prepare(`INSERT INTO alpaca_watchlist (id, symbol, name, source_stock_id, copied_fx_rate) VALUES (1, 'AAPL', 'Apple', 1, 1)`).run();
    return db;
}

test('price levels need a side and a positive price, and get unique source ids', t => {
    const db = makeDb(t);
    const first = levels.addPriceLevel(db, 1, { side: 'buy', price_usd: 100 });
    const second = levels.addPriceLevel(db, 1, { side: 'SELL', price_usd: 120 });
    assert.equal(first.side, 'BUY');
    assert.equal(first.trigger_mode, 'PRICE');
    assert.notEqual(first.source_alert_level_id, second.source_alert_level_id);
    assert.throws(() => levels.addPriceLevel(db, 1, { side: 'HOLD', price_usd: 1 }), /BUY or SELL/);
    assert.throws(() => levels.addPriceLevel(db, 1, { side: 'BUY', price_usd: 0 }), /positive/);
    assert.throws(() => levels.addPriceLevel(db, 99, { side: 'BUY', price_usd: 1 }), /not found/);
});

test('percentage levels derive their target from the reference quote', t => {
    const db = makeDb(t);
    const drop = levels.addPercentLevel(db, 1, { direction: 'DROP', change_pct: 2, reference_price_usd: 100 });
    assert.equal(drop.side, 'BUY');
    assert.equal(Number(drop.price_usd).toFixed(2), '98.00');
    const gain = levels.addPercentLevel(db, 1, { direction: 'GAIN', change_pct: 1, reference_price_usd: 100 });
    assert.equal(gain.side, 'SELL');
    assert.equal(Number(gain.price_usd).toFixed(2), '101.00');
    assert.throws(() => levels.addPercentLevel(db, 1, { direction: 'DROP', change_pct: 2 }), /quote/);
    assert.throws(() => levels.addPercentLevel(db, 1, { direction: 'DROP', change_pct: 100, reference_price_usd: 100 }), /below/);
});

test('editing a percentage move recalculates its target; retired levels must be rearmed', t => {
    const db = makeDb(t);
    const drop = levels.addPercentLevel(db, 1, { direction: 'DROP', change_pct: 2, reference_price_usd: 100 });
    const edited = levels.updateLevel(db, drop.id, { change_pct: 5 });
    assert.equal(Number(edited.price_usd).toFixed(2), '95.00');
    assert.throws(() => levels.updateLevel(db, drop.id, { price_usd: 90 }), /calculated/);
    db.prepare("UPDATE alpaca_alert_levels SET triggered=1, retired=1, enabled=0, active=0 WHERE id=?").run(drop.id);
    assert.throws(() => levels.updateLevel(db, drop.id, { enabled: true }), /rearmed explicitly/);
    assert.equal(levels.updateLevel(db, drop.id, { price_usd: undefined, enabled: false }).enabled, 0);
});

test('a BUY needs a budget before real orders switch on; a SELL does not', t => {
    const db = makeDb(t);
    const buy = levels.addPriceLevel(db, 1, { side: 'BUY', price_usd: 100 });
    const sell = levels.addPriceLevel(db, 1, { side: 'SELL', price_usd: 120 });
    assert.throws(() => levels.setOrderSettings(db, buy.id, { auto_order: true }), /needs an order budget/);
    assert.throws(() => levels.setOrderSettings(db, buy.id, { auto_order: true, budget_usd: -5 }), /positive/);
    const armed = levels.setOrderSettings(db, buy.id, { auto_order: true, budget_usd: 300 });
    assert.equal(armed.auto_order, 1);
    assert.equal(armed.order_budget_usd, 300);
    assert.equal(levels.setOrderSettings(db, sell.id, { auto_order: true }).auto_order, 1);
    const off = levels.setOrderSettings(db, buy.id, { auto_order: false });
    assert.equal(off.auto_order, 0);
    assert.equal(off.order_budget_usd, 300, 'budget is kept when real orders are switched off');
});

test('a level with a live order cannot be deleted; a quiet one can', t => {
    const db = makeDb(t);
    const level = levels.addPriceLevel(db, 1, { side: 'SELL', price_usd: 120 });
    db.prepare(`INSERT INTO alpaca_orders (alpaca_alert_level_id, client_order_id, symbol, side, qty, limit_price_usd, notional_usd, status)
        VALUES (?, 'os-test', 'AAPL', 'SELL', 1, 120, 120, 'NEW')`).run(level.id);
    assert.throws(() => levels.deleteLevel(db, level.id), /Cancel the open order/);
    db.prepare("UPDATE alpaca_orders SET status='FILLED' WHERE alpaca_alert_level_id=?").run(level.id);
    assert.deepEqual(levels.deleteLevel(db, level.id), { deleted_id: level.id });
    assert.throws(() => levels.deleteLevel(db, level.id), /not found/);
});
