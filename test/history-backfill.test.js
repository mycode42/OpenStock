const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const Database = require('better-sqlite3');
const OpenStockDB = require('../database/db');
const { HistoryBackfillQueue } = require('../lib/history-backfill');
const { yahooChartUrl } = require('../lib/yahoo-chart');

function makeDb() {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-history-'));
    const file = path.join(dir, 'test.db');
    const raw = new Database(file);
    raw.exec(fs.readFileSync(path.join(__dirname, '..', 'database/schema.sql'), 'utf8'));
    raw.close();
    return { db: new OpenStockDB(file), dir };
}

function chart() {
    return { chart: { result: [{ timestamp: [1767225600, 1767312000], indicators: { quote: [{ close: [101, 102] }] } }] } };
}

test('backfills only active stocks, uses one 5y daily request, and is idempotent', async t => {
    const { db, dir } = makeDb();
    t.after(() => { db.db.close(); fs.rmSync(dir, { recursive: true, force: true }); });
    const active = Number(db.addStock('ACTIVE', 'Active', 'USD', 1, 2).lastInsertRowid);
    const disabled = Number(db.addStock('DISABLED', 'Disabled', 'USD', 1, 2).lastInsertRowid);
    db.updateStock(disabled, { enabled: 0 });
    const archived = Number(db.addStock('ARCHIVED', 'Archived', 'USD', 1, 2).lastInsertRowid);
    db.updateStock(archived, { archived: 1, enabled: 0 });

    const calls = [];
    const queue = new HistoryBackfillQueue({
        db,
        delayMs: 0,
        fetchImpl: async (url) => { calls.push(url); return { ok: true, json: async () => chart() }; },
        logger: { log() {}, error: assert.fail }
    });
    assert.equal(queue.enqueueMissing(), 1);
    await queue.run();
    assert.deepEqual(calls, [yahooChartUrl('ACTIVE', { range: '5y', interval: '1d' })]);
    assert.equal(db.db.prepare("SELECT COUNT(*) AS count FROM price_history WHERE stock_id = ? AND source = 'yahoo_backfill'").get(active).count, 2);
    assert.equal(queue.enqueueMissing(), 0);
});

test('history sampling returns the latest observation from each integer time bucket', t => {
    const { db, dir } = makeDb();
    t.after(() => { db.db.close(); fs.rmSync(dir, { recursive: true, force: true }); });
    const stockId = Number(db.addStock('SAMPLE', 'Sample', 'USD', 1, 2).lastInsertRowid);
    db.addPriceSnapshot(stockId, 100, null, null, 'monitor', null, '2026-01-01 10:00:00');
    db.addPriceSnapshot(stockId, 101, null, null, 'monitor', null, '2026-01-01 15:00:00');
    db.addPriceSnapshot(stockId, 102, null, null, 'monitor', null, '2026-01-02 12:00:00');
    const points = db.getPriceHistory(stockId, '-10 years', 86400);
    assert.equal(points.length, 2);
    assert.deepEqual(points.map(point => point.price_usd), [101, 102]);
});

test('historical writes do not overwrite an existing monitor observation and failures are isolated', async t => {
    const { db, dir } = makeDb();
    t.after(() => { db.db.close(); fs.rmSync(dir, { recursive: true, force: true }); });
    const good = Number(db.addStock('GOOD', 'Good', 'USD', 1, 2).lastInsertRowid);
    db.addStock('BAD', 'Bad', 'USD', 1, 2);
    db.addPriceSnapshot(good, 99, 90, 1, 'monitor', null, '2026-01-01 00:00:00');
    const errors = [];
    const queue = new HistoryBackfillQueue({
        db,
        delayMs: 0,
        fetchImpl: async url => {
            if (url.includes('/BAD?')) throw new Error('mock offline');
            return { ok: true, json: async () => chart() };
        },
        logger: { log() {}, error: message => errors.push(message) }
    });
    queue.enqueueMissing();
    await queue.run();
    assert.equal(db.db.prepare("SELECT source FROM price_history WHERE stock_id = ? AND recorded_at = '2026-01-01 00:00:00'").get(good).source, 'monitor');
    assert.equal(errors.length, 1);
    assert.equal(db.db.prepare("SELECT COUNT(*) AS count FROM price_history WHERE source = 'yahoo_backfill'").get().count, 1);
});
