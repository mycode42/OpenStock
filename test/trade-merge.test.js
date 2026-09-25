'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const OpenStockDB = require('../database/db');
const { initDatabase } = require('../database/init');
const { calculateDashboard } = require('../lib/analytics');

function fixture() {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-trade-merge-'));
    const file = path.join(dir, 'db.sqlite');
    initDatabase({ dbPath: file, seedDefaults: false, silent: true });
    const db = new OpenStockDB(file);
    db.addStock('MERGE', 'Merge', 'USD', 10, 20);
    return { dir, db };
}

function trade(overrides = {}) {
    return {
        trade_id: 'T-1', stock_id: 1, opened_at: '2026-09-14 10:00:00', closed_at: null,
        quantity: 1, currency: 'EUR', entry_price: 10, entry_fx_rate: 1, entry_price_eur: 10,
        entry_fee_eur: 1, exit_price: null, exit_fx_rate: null, exit_price_eur: null,
        exit_fee_eur: 0, dividend_eur: 0, strategy: '', notes: '', ...overrides
    };
}

test('a new same-stock purchase merges into the existing open trade without changing accounting', t => {
    const { dir, db } = fixture();
    t.after(() => { db.db.close(); fs.rmSync(dir, { recursive: true, force: true }); });
    const first = db.addTrade(trade());
    const second = db.addTrade(trade({ trade_id: 'T-2', opened_at: '2026-09-14 10:05:00', quantity: 3, entry_price: 20, entry_price_eur: 20, entry_fee_eur: 2, notes: 'second buy' }));
    assert.equal(first.merged, false);
    assert.equal(second.merged, true);
    assert.equal(second.lastInsertRowid, first.lastInsertRowid);
    const rows = db.getAllTrades({ status: 'open' });
    assert.equal(rows.length, 1);
    assert.equal(rows[0].quantity, 4);
    assert.equal(rows[0].entry_price_eur, 17.5);
    assert.equal(rows[0].entry_price, 17.5);
    assert.equal(rows[0].entry_fx_rate, 1);
    assert.equal(rows[0].entry_fee_eur, 3);
    assert.equal(rows[0].opened_at, '2026-09-14 10:00:00');
    assert.match(rows[0].notes, /second buy/);
    const audit = db.getTradeMergeEvents(rows[0].id);
    assert.equal(audit.length, 1);
    assert.equal(audit[0].source_trade_id, 'T-2');
    assert.equal(JSON.parse(audit[0].source_trade_json).quantity, 3);

    const analytics = calculateDashboard({ trades: rows, stocks: [{ id: 1, current_price_eur: 25 }] });
    assert.equal(analytics.capital.invested_eur, 100);
    assert.equal(analytics.realized.total_fees_eur, 3);
    assert.equal(analytics.capital.unrealized_pnl_eur, 27);
});

test('merged unit prices are rounded to four decimal places', t => {
    const { dir, db } = fixture();
    t.after(() => { db.db.close(); fs.rmSync(dir, { recursive: true, force: true }); });
    db.addTrade(trade({ trade_id: 'R-1', quantity: 1, entry_price: 44.82, entry_price_eur: 44.82 }));
    db.addTrade(trade({ trade_id: 'R-2', quantity: 2, entry_price: 44.85, entry_price_eur: 44.85 }));
    const merged = db.getAllTrades({ status: 'open' })[0];
    assert.equal(merged.entry_price, 44.84);
    assert.equal(merged.entry_price_eur, 44.84);
});

test('existing duplicate open rows consolidate transactionally and retain source snapshots', t => {
    const { dir, db } = fixture();
    t.after(() => { db.db.close(); fs.rmSync(dir, { recursive: true, force: true }); });
    const insert = db.db.prepare(`INSERT INTO trades (trade_id,stock_id,opened_at,quantity,currency,entry_price,entry_fx_rate,entry_price_eur,entry_fee_eur,strategy,notes) VALUES (@trade_id,@stock_id,@opened_at,@quantity,@currency,@entry_price,@entry_fx_rate,@entry_price_eur,@entry_fee_eur,@strategy,@notes)`);
    insert.run(trade());
    insert.run(trade({ trade_id: 'T-2', opened_at: '2026-09-14 10:05:00', quantity: 2, entry_price: 13, entry_price_eur: 13, entry_fee_eur: 0.5 }));
    const result = db.consolidateOpenTrades(1);
    assert.equal(result.merged, true);
    assert.equal(result.mergedCount, 1);
    assert.equal(db.getAllTrades({ status: 'open' }).length, 1);
    assert.equal(db.getTradeMergeEvents(result.lastInsertRowid).length, 1);
    assert.throws(() => db.addTrade(trade({ trade_id: 'T-2' })), /Trade ID already exists/);
});

test('closed historical trades remain separate', t => {
    const { dir, db } = fixture();
    t.after(() => { db.db.close(); fs.rmSync(dir, { recursive: true, force: true }); });
    db.addTrade(trade());
    const result = db.addTrade(trade({ trade_id: 'CLOSED', closed_at: '2026-09-15 10:00:00', exit_price: 12, exit_fx_rate: 1, exit_price_eur: 12 }));
    assert.equal(result.merged, false);
    const rows = db.getAllTrades();
    assert.equal(rows.length, 2);
    assert.deepEqual(rows.map(row => row.trade_number), [2, 1], 'display numbers increment by trade creation order even when the newest trade is listed first');
});
