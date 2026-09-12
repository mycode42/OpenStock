const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const Database = require('better-sqlite3');
const OpenStockDB = require('../database/db');

function makeDb() {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-'));
    const file = path.join(dir, 'test.db');
    const raw = new Database(file);
    raw.exec(fs.readFileSync(path.join(__dirname, '..', 'database/schema.sql'), 'utf8'));
    raw.close();
    return { db: new OpenStockDB(file), dir };
}

test('removing a stock with journal history archives it and preserves readable history', t => {
    const { db, dir } = makeDb();
    t.after(() => { db.db.close(); fs.rmSync(dir, { recursive: true, force: true }); });
    const stockId = Number(db.addStock('OLD', 'Old Corp', 'USD', 10, 20).lastInsertRowid);
    db.addTrade({
        trade_id: 'TRADE-OLD', stock_id: stockId, opened_at: '2026-01-01 00:00:00', closed_at: null,
        quantity: 1, currency: 'USD', entry_price: 10, entry_fx_rate: 1, entry_price_eur: 10,
        entry_fee_eur: 0, exit_price: null, exit_fx_rate: null, exit_price_eur: null, exit_fee_eur: 0,
        dividend_eur: 0, strategy: '', notes: ''
    });

    const result = db.removeStock(stockId);
    assert.equal(result.archived, true);
    assert.equal(db.getConfigStocks().length, 0);
    assert.equal(db.getActiveStocks().length, 0);
    assert.equal(db.getJournalStocks().find(stock => stock.id === stockId).archived, 1);
    assert.equal(db.getAllTrades()[0].symbol, 'OLD');
});

test('removing a stock without journal history deletes it', t => {
    const { db, dir } = makeDb();
    t.after(() => { db.db.close(); fs.rmSync(dir, { recursive: true, force: true }); });
    const stockId = Number(db.addStock('EMPTY', 'Empty Corp', 'USD', 10, 20).lastInsertRowid);
    const result = db.removeStock(stockId);
    assert.equal(result.archived, false);
    assert.equal(db.getStockById(stockId), undefined);
});

test('archived rows do not occupy the configuration catalogue', t => {
    const { db, dir } = makeDb();
    t.after(() => { db.db.close(); fs.rmSync(dir, { recursive: true, force: true }); });
    const first = Number(db.addStock('FIRST', 'First Corp', 'USD', 10, 20).lastInsertRowid);
    const second = Number(db.addStock('SECOND', 'Second Corp', 'USD', 10, 20).lastInsertRowid);
    db.updateStock(first, { archived: 1, enabled: 0 });
    assert.deepEqual(db.getConfigStocks().map(stock => stock.symbol), ['SECOND']);
    assert.deepEqual(db.getActiveStocks().map(stock => stock.symbol), ['SECOND']);
    assert.equal(db.getAllStocks().length, 2);
    assert.equal(second > first, true);
});

test('an archived symbol can be restored without losing its journal identity', t => {
    const { db, dir } = makeDb();
    t.after(() => { db.db.close(); fs.rmSync(dir, { recursive: true, force: true }); });
    const stockId = Number(db.addStock('RETURN', 'Old Name', 'USD', 10, 20).lastInsertRowid);
    db.addTrade({
        trade_id: 'TRADE-RETURN', stock_id: stockId, opened_at: '2026-01-01 00:00:00', closed_at: null,
        quantity: 1, currency: 'USD', entry_price: 10, entry_fx_rate: 1, entry_price_eur: 10,
        entry_fee_eur: 0, exit_price: null, exit_fx_rate: null, exit_price_eur: null, exit_fee_eur: 0,
        dividend_eur: 0, strategy: '', notes: ''
    });
    db.removeStock(stockId);

    const result = db.restoreArchivedStock(stockId, { name: 'New Name', currency: 'USD', buyThreshold: 12, sellThreshold: 24 });
    const restored = db.getStockById(stockId);
    assert.equal(result.changes, 1);
    assert.equal(restored.archived, 0);
    assert.equal(restored.enabled, 1);
    assert.equal(restored.name, 'New Name');
    assert.equal(restored.buy_threshold_eur, 12);
    assert.equal(db.getAllTrades()[0].stock_id, stockId);
});
