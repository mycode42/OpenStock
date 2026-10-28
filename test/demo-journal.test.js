const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { initDatabase } = require('../database/init');
const OpenStockDB = require('../database/db');
const { calculateDashboard } = require('../lib/analytics');
const { seedDemoJournal, LOTS } = require('../server/demo-journal');

function tempDb() {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-demo-journal-'));
    const dbPath = path.join(dir, 'demo.db');
    initDatabase({ dbPath, silent: true });
    const db = new OpenStockDB(dbPath);
    return { db, cleanup: () => { db.db.close(); fs.rmSync(dir, { recursive: true, force: true }); } };
}

test('seeds trades, capital and a snapshot curve into an empty demo database', () => {
    const { db, cleanup } = tempDb();
    try {
        const now = new Date('2026-10-28T12:00:00Z');
        assert.equal(seedDemoJournal({ db, now }), true);

        const trades = db.getAllTrades();
        assert.equal(trades.length, LOTS.length);
        assert.equal(trades.filter(trade => trade.closed_at).length, 4);
        assert.equal(trades.filter(trade => !trade.closed_at).length, 3);
        assert.deepEqual(db.getCapitalEvents().map(event => event.event_type), ['INITIAL', 'DEPOSIT']);

        const snapshots = db.getEquitySnapshots();
        assert.ok(snapshots.length >= 150, `expected a curve of at least 150 points, got ${snapshots.length}`);

        const analytics = calculateDashboard({
            stocks: db.getAllStocks(),
            trades,
            capitalEvents: db.getCapitalEvents(),
            snapshots,
            now: now.toISOString(),
        });
        assert.equal(analytics.equity_available, true);
        assert.ok(analytics.capital.current_eur > 0);
        const last = snapshots[snapshots.length - 1];
        assert.ok(Math.abs(last.equity_eur - analytics.capital.current_eur) < 1e-6, 'final snapshot matches live equity');
    } finally {
        cleanup();
    }
});

test('does not seed again once the journal has data', () => {
    const { db, cleanup } = tempDb();
    try {
        const now = new Date('2026-10-28T12:00:00Z');
        assert.equal(seedDemoJournal({ db, now }), true);
        const before = db.getAllTrades().length;
        assert.equal(seedDemoJournal({ db, now }), false);
        assert.equal(db.getAllTrades().length, before);
    } finally {
        cleanup();
    }
});
