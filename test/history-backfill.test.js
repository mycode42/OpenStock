const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const Database = require('better-sqlite3');
const OpenStockDB = require('../database/db');
const { HistoryBackfillQueue } = require('../lib/history-backfill');
const { yahooChartUrl } = require('../lib/yahoo-chart');
const { initDatabase } = require('../database/init');

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

test('verified mappings backfill through Trade Republic without Yahoo', async t => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-history-tr-auto-'));
    const file = path.join(dir, 'test.db');
    initDatabase({ dbPath: file, seedDefaults: false, silent: true });
    const db = new OpenStockDB(file);
    t.after(() => { db.db.close(); fs.rmSync(dir, { recursive: true, force: true }); });
    const stockId = Number(db.addStock('EURX', 'Euro stock', 'EUR', 1, 2, 'US0000000001').lastInsertRowid);
    db.saveBrokerMapping({ stock_id: stockId, broker: 'trade_republic', ticker: 'EURX', isin: 'US0000000001', exchange: 'TIB', currency: 'EUR', broker_metadata_json: '{}', source: 'broker_verified', verified_at: '2026-01-01T00:00:00Z' });
    const brokerCalls = [];
    let yahooCalls = 0;
    const queue = new HistoryBackfillQueue({
        db,
        delayMs: 0,
        fetchImpl: async () => { yahooCalls++; throw new Error('Yahoo must not be called for a verified Trade Republic mapping'); },
        brokerClient: { getInstrumentHistory: async request => { brokerCalls.push(request); return { range: 'max', resolution: 86400000, points: [{ recordedAt: '2026-01-01 00:00:00', quoteTime: '2026-01-01 00:00:00', priceEur: 101, priceUsd: null, exchangeRate: null }] }; } },
        logger: { log() {}, error: assert.fail }
    });
    assert.equal(queue.enqueueMissing(), 1);
    await queue.run();
    assert.deepEqual(brokerCalls, [{ isin: 'US0000000001', range: '5y' }]);
    assert.equal(yahooCalls, 0);
    assert.deepEqual(db.db.prepare('SELECT price_eur, price_usd, source FROM price_history WHERE stock_id=?').all(stockId), [{ price_eur: 101, price_usd: null, source: 'trade_republic_history' }]);
});

test('verified mappings fail closed when Trade Republic history is unavailable', async t => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-history-tr-failure-'));
    const file = path.join(dir, 'test.db');
    initDatabase({ dbPath: file, seedDefaults: false, silent: true });
    const db = new OpenStockDB(file);
    t.after(() => { db.db.close(); fs.rmSync(dir, { recursive: true, force: true }); });
    const stockId = Number(db.addStock('EURF', 'Euro failure', 'EUR', 1, 2, 'US0000000002').lastInsertRowid);
    db.saveBrokerMapping({ stock_id: stockId, broker: 'trade_republic', ticker: 'EURF', isin: 'US0000000002', exchange: 'TIB', currency: 'EUR', broker_metadata_json: '{}', source: 'broker_verified', verified_at: '2026-01-01T00:00:00Z' });
    let yahooCalls = 0;
    const errors = [];
    const queue = new HistoryBackfillQueue({
        db,
        delayMs: 0,
        fetchImpl: async () => { yahooCalls++; return { ok: true, json: async () => chart() }; },
        brokerClient: { getInstrumentHistory: async () => { throw Object.assign(new Error('sidecar offline'), { code: 'BROKER_OFFLINE' }); } },
        logger: { log() {}, error: message => errors.push(message) }
    });
    assert.equal(queue.enqueueMissing(), 1);
    await queue.run();
    assert.equal(yahooCalls, 0);
    assert.equal(db.db.prepare('SELECT COUNT(*) count FROM price_history WHERE stock_id=?').get(stockId).count, 0);
    assert.match(errors.join('\n'), /EURF: history backfill failed: BROKER_OFFLINE/);
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

test('forced Trade Republic sync augments and authoritatively updates existing Yahoo rows', async t => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-history-tr-'));
    const file = path.join(dir, 'test.db');
    initDatabase({ dbPath: file, seedDefaults: false, silent: true });
    const db = new OpenStockDB(file);
    t.after(() => { db.db.close(); fs.rmSync(dir, { recursive: true, force: true }); });
    const stockId = Number(db.addStock('BIRK', 'Birkenstock', 'EUR', 1, 2, 'JE00BS44BN30').lastInsertRowid);
    db.saveBrokerMapping({ stock_id: stockId, broker: 'trade_republic', ticker: 'BIRK', isin: 'JE00BS44BN30', exchange: 'TIB', currency: 'EUR', broker_metadata_json: '{}', source: 'broker_verified', verified_at: '2026-01-01T00:00:00Z' });
    db.addHistoricalPriceSnapshots(stockId, [{ recordedAt: '2026-01-01 00:00:00', quoteTime: '2026-01-01 00:00:00', priceUsd: 110, priceEur: 100, exchangeRate: 0.9 }], 'yahoo_backfill');
    const queue = new HistoryBackfillQueue({ db, delayMs: 0, brokerClient: { getInstrumentHistory: async () => ({ range: 'max', resolution: 86400000, points: [
        { recordedAt: '2026-01-01 00:00:00', quoteTime: '2026-01-01 00:00:00', priceEur: 101, priceUsd: null, exchangeRate: null },
        { recordedAt: '2026-01-02 00:00:00', quoteTime: '2026-01-02 00:00:00', priceEur: 102, priceUsd: null, exchangeRate: null }
    ] }) }, logger: { log() {}, error: assert.fail } });
    const result = await queue.syncTradeRepublic(db.getStockById(stockId));
    assert.equal(result.points, 2);
    const rows = db.db.prepare('SELECT recorded_at, price_eur, price_usd, source FROM price_history WHERE stock_id=? ORDER BY recorded_at').all(stockId);
    assert.deepEqual(rows, [{ recorded_at: '2026-01-01 00:00:00', price_eur: 101, price_usd: null, source: 'trade_republic_history' }, { recorded_at: '2026-01-02 00:00:00', price_eur: 102, price_usd: null, source: 'trade_republic_history' }]);
});
