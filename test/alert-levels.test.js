const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const OpenStockDB = require('../database/db');
const { initDatabase } = require('../database/init');

function fixture() {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-levels-'));
    const file = path.join(dir, 'db.sqlite');
    initDatabase({ dbPath: file, seedDefaults: false, silent: true });
    return { dir, file };
}

test('migration creates exactly one legacy BUY and SELL level and remains idempotent', t => {
    const { dir, file } = fixture(); t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
    const db = new OpenStockDB(file);
    db.addStock('TEST', 'Test', 'USD', 10, 20);
    assert.deepEqual(db.getAlertLevels(1).map(level => [level.side, level.price_eur]), [['BUY', 10], ['SELL', 20]]);
    db.db.close(); initDatabase({ dbPath: file, seedDefaults: false, silent: true });
    const check = new OpenStockDB(file);
    assert.equal(check.getAlertLevels(1).length, 2);
    check.db.close();
});

test('positions are exclusively derived from open Journal lots', t => {
    const { dir, file } = fixture(); t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
    const db = new OpenStockDB(file);
    db.addStock('JOURNAL', 'Journal', 'USD', 10, 20);
    assert.deepEqual(db.getPositionSummary(1), { quantity: 0, owned: false, source: 'journal', warning: 'No open Journal position; SELL alerts are suppressed.' });
    db.addTrade({ trade_id: 'T1', stock_id: 1, opened_at: '2026-01-01 00:00:00', closed_at: null, quantity: 3, currency: 'EUR', entry_price: 10, entry_fx_rate: 1, entry_price_eur: 10, entry_fee_eur: 0, exit_price: null, exit_fx_rate: null, exit_price_eur: null, exit_fee_eur: 0, dividend_eur: 0, strategy: '', notes: '' });
    assert.deepEqual(db.getPositionSummary(1), { quantity: 3, owned: true, source: 'journal', warning: null });
    db.db.close();
});

test('journal history with no open lots never falls back to manual ownership', t => {
    const { dir, file } = fixture(); t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
    const db = new OpenStockDB(file);
    db.addStock('CLOSED', 'Closed', 'USD', 10, 20);
    db.addTrade({ trade_id: 'C1', stock_id: 1, opened_at: '2026-01-01 00:00:00', closed_at: '2026-01-02 00:00:00', quantity: 2, currency: 'EUR', entry_price: 10, entry_fx_rate: 1, entry_price_eur: 10, entry_fee_eur: 0, exit_price: 12, exit_fx_rate: 1, exit_price_eur: 12, exit_fee_eur: 0, dividend_eur: 0, strategy: '', notes: '' });
    assert.deepEqual(db.getPositionSummary(1), { quantity: 0, owned: false, source: 'journal', warning: 'No open Journal position; SELL alerts are suppressed.' });
    db.db.close();
});

test('alert levels are independently rearmable and state transitions are normalized', t => {
    const { dir, file } = fixture(); t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
    const db = new OpenStockDB(file);
    db.addStock('LEVEL', 'Level', 'USD', 10, 20);
    const level = db.addAlertLevel(1, 'BUY', 9);
    assert.equal(db.updateAlertLevel(level.id, { enabled: false }).active, 0);
    db.retireAlertLevel(level.id);
    const retired = db.getAlertLevelById(level.id);
    assert.deepEqual({ enabled: retired.enabled, active: retired.active, triggered: retired.triggered, retired: retired.retired }, { enabled: 0, active: 0, triggered: 1, retired: 1 });
    assert.throws(() => db.updateAlertLevel(level.id, { enabled: true }), /rearmed explicitly/);
    assert.equal(db.rearmAlertLevel(level.id).retired, 0);
    assert.equal(db.updateAlertLevel(level.id, { side: 'SELL', price_eur: 21, enabled: true }).active, 1);
    assert.throws(() => db.updateAlertLevel(level.id, { enabled: 'yes' }), /boolean/);
    db.db.close();
});
