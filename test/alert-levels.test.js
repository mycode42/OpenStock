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
    assert.deepEqual(db.getPositionSummary(1), { quantity: 0, owned: false, source: 'journal', buy_in_price_eur: null, warning: 'No open Journal position; SELL alerts are suppressed.' });
    db.addTrade({ trade_id: 'T1', stock_id: 1, opened_at: '2026-01-01 00:00:00', closed_at: null, quantity: 3, currency: 'EUR', entry_price: 10, entry_fx_rate: 1, entry_price_eur: 10, entry_fee_eur: 0, exit_price: null, exit_fx_rate: null, exit_price_eur: null, exit_fee_eur: 0, dividend_eur: 0, strategy: '', notes: '' });
    db.addTrade({ trade_id: 'T2', stock_id: 1, opened_at: '2026-01-02 00:00:00', closed_at: null, quantity: 1, currency: 'EUR', entry_price: 16, entry_fx_rate: 1, entry_price_eur: 16, entry_fee_eur: 0, exit_price: null, exit_fx_rate: null, exit_price_eur: null, exit_fee_eur: 0, dividend_eur: 0, strategy: '', notes: '' });
    assert.deepEqual(db.getPositionSummary(1), { quantity: 4, owned: true, source: 'journal', buy_in_price_eur: 11.5, warning: null });
    db.db.close();
});

test('journal history with no open lots never falls back to manual ownership', t => {
    const { dir, file } = fixture(); t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
    const db = new OpenStockDB(file);
    db.addStock('CLOSED', 'Closed', 'USD', 10, 20);
    db.addTrade({ trade_id: 'C1', stock_id: 1, opened_at: '2026-01-01 00:00:00', closed_at: '2026-01-02 00:00:00', quantity: 2, currency: 'EUR', entry_price: 10, entry_fx_rate: 1, entry_price_eur: 10, entry_fee_eur: 0, exit_price: 12, exit_fx_rate: 1, exit_price_eur: 12, exit_fee_eur: 0, dividend_eur: 0, strategy: '', notes: '' });
    assert.deepEqual(db.getPositionSummary(1), { quantity: 0, owned: false, source: 'journal', buy_in_price_eur: null, warning: 'No open Journal position; SELL alerts are suppressed.' });
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

test('percentage alerts keep a fixed reference and derive the target from the requested move', t => {
    const { dir, file } = fixture(); t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
    const db = new OpenStockDB(file);
    db.addStock('MOVE', 'Move', 'EUR', 80, 120);
    db.db.prepare('UPDATE stocks SET current_price_eur=100 WHERE id=1').run();

    const drop = db.addPercentAlertLevel(1, 'DROP', 5);
    const gain = db.addPercentAlertLevel(1, 'GAIN', 10);
    assert.deepEqual([drop.trigger_mode, drop.side, drop.change_pct, drop.reference_price_eur, drop.price_eur], ['PERCENT', 'BUY', 5, 100, 95]);
    assert.deepEqual([gain.trigger_mode, gain.side, gain.change_pct, gain.reference_price_eur, gain.price_eur], ['PERCENT', 'SELL', 10, 100, 110]);
    const adjusted = db.updateAlertLevel(drop.id, { change_pct: 7.5 });
    assert.equal(adjusted.reference_price_eur, 100);
    assert.equal(adjusted.price_eur, 92.5);
    assert.throws(() => db.addPercentAlertLevel(1, 'DROP', 100), /maximum/);
    db.db.close();
});

test('rearming a delivered level clears its prior outbox occurrence but preserves alert history', t => {
    const { dir, file } = fixture(); t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
    const db = new OpenStockDB(file);
    db.addStock('REARM', 'Rearm', 'USD', 10, 20);
    const level = db.getAlertLevels(1).find(item => item.side === 'SELL');
    db.db.prepare("INSERT INTO alerts_log(stock_id,alert_level_id,alert_type,price_eur,threshold_eur,telegram_message_id) VALUES (1,?,'SELL',20,20,99)").run(level.id);
    db.db.prepare("INSERT INTO alert_outbox(alert_level_id,delivery_key,stock_id,message_html,price_eur,status,telegram_message_id) VALUES (?, 'level:test', 1, 'old delivery', 20, 'SENT', '99')").run(level.id);
    db.retireAlertLevel(level.id);

    const rearmed = db.rearmAlertLevel(level.id);
    assert.deepEqual({ enabled: rearmed.enabled, active: rearmed.active, triggered: rearmed.triggered, retired: rearmed.retired }, { enabled: 1, active: 1, triggered: 0, retired: 0 });
    assert.equal(db.db.prepare('SELECT COUNT(*) AS count FROM alert_outbox WHERE alert_level_id=?').get(level.id).count, 0);
    assert.equal(db.db.prepare('SELECT COUNT(*) AS count FROM alerts_log WHERE alert_level_id=?').get(level.id).count, 1);
    db.db.close();
});
