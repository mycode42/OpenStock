'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { initDatabase } = require('../database/init');
const OpenStockDB = require('../database/db');
const { HistoryBackfillQueue } = require('../lib/history-backfill');
const { QuoteService } = require('../lib/quote-service');

function fixture() {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-history-'));
    const file = path.join(dir, 'test.db');
    initDatabase({ dbPath: file, seedDefaults: false, silent: true });
    return { dir, db: new OpenStockDB(file) };
}

function addMappedStock(db, symbol) {
    const id = Number(db.addStock(symbol, symbol, 'EUR', 1, 2).lastInsertRowid);
    db.saveBrokerMapping({ stock_id: id, broker: 'trade_republic', ticker: symbol, isin: `US${String(id).padStart(10, '0')}`, exchange: 'TIB', currency: 'EUR', broker_metadata_json: '{}', source: 'test', verified_at: '2026-01-01T00:00:00Z' });
    return id;
}

function history(range, value = 1.234567) {
    const day = { '5y': 1, '1y': 2, '1mo': 3, '1d': 4 }[range];
    const recordedAt = `2026-01-0${day} 00:00:00`;
    return { range, resolution: 3600, points: [{ recordedAt, quoteTime: recordedAt, priceEur: value, priceUsd: null, exchangeRate: null }] };
}

test('history queue processes active mapped stocks and is idempotent', async t => {
    const { dir, db } = fixture();
    t.after(() => { db.db.close(); fs.rmSync(dir, { recursive: true, force: true }); });
    const activeId = addMappedStock(db, 'ACTIVE');
    const inactiveId = addMappedStock(db, 'DISABLED');
    db.updateStock(inactiveId, { enabled: 0 });
    db.addStock('UNMAPPED', 'Unmapped', 'EUR', 1, 2);
    const requests = [];
    const queue = new HistoryBackfillQueue({ db, delayMs: 0, brokerClient: { getInstrumentHistory: async request => { requests.push(request); return history(request.range); } }, logger: { log() {}, error: assert.fail } });

    assert.equal(queue.enqueueMissing(), 1);
    await queue.run();
    assert.deepEqual(requests, ['5y', '1y', '1mo', '1d'].map(range => ({ isin: `US${String(activeId).padStart(10, '0')}`, exchange: 'TIB', range, assetType: 'stock' })));
    assert.equal(db.db.prepare("SELECT COUNT(*) count FROM price_history WHERE stock_id=? AND source='trade_republic_history'").get(activeId).count, 4);
    assert.equal(queue.enqueueMissing(), 0);
});

test('crypto history uses the SDK candle reader instead of the stock sidecar', async t => {
    const { dir, db } = fixture();
    t.after(() => { db.db.close(); fs.rmSync(dir, { recursive: true, force: true }); });
    const id = addMappedStock(db, 'ETH');
    db.db.prepare("UPDATE stocks SET asset_type='crypto' WHERE id=?").run(id);
    const sdkRequests = [];
    const queue = new HistoryBackfillQueue({
        db, delayMs: 0,
        brokerClient: { getInstrumentHistory: async () => { assert.fail('crypto history should use SDK'); } },
        sdkClient: { getInstrumentHistory: async request => { sdkRequests.push(request); return history(request.range); } },
        logger: { log() {}, error: assert.fail }
    });
    queue.enqueueMissing();
    await queue.run();
    assert.equal(sdkRequests.length, 4);
    assert.equal(sdkRequests[0].isin, `US${String(id).padStart(10, '0')}`);
    assert.equal(sdkRequests[0].exchange, 'TIB');
    assert.equal(db.db.prepare("SELECT COUNT(*) count FROM price_history WHERE stock_id=? AND source='trade_republic_history'").get(id).count, 4);
});

test('history failures are recorded and isolated to the affected symbol', async t => {
    const { dir, db } = fixture();
    t.after(() => { db.db.close(); fs.rmSync(dir, { recursive: true, force: true }); });
    const badId = addMappedStock(db, 'BAD');
    const goodId = addMappedStock(db, 'GOOD');
    const errors = [];
    const queue = new HistoryBackfillQueue({ db, delayMs: 0, brokerClient: { getInstrumentHistory: async ({ isin, range }) => {
        if (isin.endsWith(String(badId).padStart(10, '0'))) throw Object.assign(new Error('sidecar offline'), { code: 'BROKER_OFFLINE' });
        return history(range);
    } }, logger: { log() {}, error: message => errors.push(message) } });

    queue.enqueueMissing();
    await queue.run();
    assert.equal(db.db.prepare("SELECT COUNT(*) count FROM price_history WHERE stock_id=?").get(badId).count, 0);
    assert.equal(db.db.prepare("SELECT COUNT(*) count FROM price_history WHERE stock_id=?").get(goodId).count, 4);
    assert.match(errors.join('\n'), /BAD: history backfill failed: BROKER_OFFLINE/);
    assert.equal(db.hasCompletedHistoryBackfill(badId, 'trade_republic', ['5y', '1y', '1mo', '1d']), false);
});

test('SDK/UAPI live persistence stores EUR values to six decimal places as REAL', t => {
    const { dir, db } = fixture();
    t.after(() => { db.db.close(); fs.rmSync(dir, { recursive: true, force: true }); });
    const id = addMappedStock(db, 'SIX');
    const stock = { ...db.getStockById(id), mapping: db.getBrokerMapping(id) };
    const service = new QuoteService({ db });
    service.persist(stock, { bidEur: 1.234567, askEur: 1.234568, priceEur: 1.2345675, quoteTime: '2026-10-01T10:00:00Z', observedAt: '2026-10-01T10:00:00Z', lastEur: null, source: 'trade_republic_sdk_order_price' }, { recordedAt: '2026-10-01 10:00:01' });

    assert.equal(db.getStockById(id).current_price_eur, 1.2345675);
    assert.deepEqual(db.db.prepare('SELECT price_eur,source FROM price_history WHERE stock_id=?').get(id), { price_eur: 1.2345675, source: 'trade_republic_sdk_order_price' });
    assert.deepEqual(db.db.prepare('SELECT bid_eur,ask_eur FROM broker_quotes WHERE isin=?').get(stock.mapping.isin), { bid_eur: 1.234567, ask_eur: 1.234568 });
});

test('historical writes update matching times and charts keep chronological EUR points', t => {
    const { dir, db } = fixture();
    t.after(() => { db.db.close(); fs.rmSync(dir, { recursive: true, force: true }); });
    const id = addMappedStock(db, 'ORDERED');
    db.addPriceSnapshot(id, null, 100, null, 'trade_republic_history', '2026-01-01T00:00:00Z', '2026-01-01 00:00:00');
    db.addHistoricalPriceSnapshots(id, [{ recordedAt: '2026-01-01 00:00:00', quoteTime: '2026-01-01T00:00:00Z', priceEur: 1.234567, priceUsd: null, exchangeRate: null }]);
    db.addHistoricalPriceSnapshots(id, [{ recordedAt: '2026-01-02 00:00:00', quoteTime: '2026-01-02T00:00:00Z', priceEur: 1.234568, priceUsd: null, exchangeRate: null }]);
    const points = db.getPriceHistory(id, '-10 years', 60, { sources: ['trade_republic_history'], eurOnly: true });
    assert.deepEqual(points.map(point => [point.price_eur, point.source]), [[1.234567, 'trade_republic_history'], [1.234568, 'trade_republic_history']]);
});
