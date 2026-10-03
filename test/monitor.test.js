'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const Database = require('better-sqlite3');
const { parseTradeRepublicMonitorQuote, sendTelegram, formatAlert, formatSdkConnectivityAlert, formatQuoteFeedAlert, checkQuoteAvailability, runMonitor, acquireLock, EXIT } = require('../lib/monitor');
const { initDatabase } = require('../database/init');

function fixture() {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-monitor-'));
    const file = path.join(dir, 'test.db');
    initDatabase({ dbPath: file, seedDefaults: false, silent: true });
    return { dir, db: new Database(file) };
}

function addMappedStock(db, { symbol, buy, sell }) {
    db.prepare('INSERT INTO stocks(symbol,name,buy_threshold_eur,sell_threshold_eur,price_source) VALUES (?,?,?,?,\'trade_republic\')').run(symbol, symbol, buy, sell);
    const id = db.prepare('SELECT id FROM stocks WHERE symbol=?').get(symbol).id;
    db.prepare("INSERT INTO broker_instrument_mappings(stock_id,broker,ticker,isin,exchange,currency,verified_at) VALUES (?, 'trade_republic', ?, ?, 'TIB', 'EUR', '2026-01-01T00:00:00Z')").run(id, symbol, `US${String(id).padStart(10, '0')}`);
    return id;
}

test('Trade Republic quote parsing keeps six-decimal EUR bid and ask values', () => {
    const quote = parseTradeRepublicMonitorQuote({ bid_eur: 1.234567, ask_eur: 1.234568, currency: 'EUR', bid_unit: 'EUR', ask_unit: 'EUR', bid_time: '2026-01-01T12:00:00Z' });
    assert.equal(quote.bidEur, 1.234567);
    assert.equal(quote.askEur, 1.234568);
    assert.equal(quote.priceEur, 1.2345675);
    assert.equal(quote.quoteTime, '2026-01-01T12:00:00.000Z');
    assert.throws(() => parseTradeRepublicMonitorQuote({ bid_eur: 1.2, currency: 'USD' }), /not EUR/);
});

test('alert text uses consistent EUR formatting and the executable side', () => {
    const quote = { source: 'trade_republic_sdk_order_price' };
    const message = formatAlert({ symbol: 'SIX', name: 'Six digits' }, 'BUY_TRIGGERED', 1.234568, new Date('2026-01-01T12:00:00Z'), { side: 'BUY', price_eur: 1.234568 }, null, quote);
    assert.match(message, /€1,234568/);
    assert.match(message, /Handelsrepublik SDK ask: €1,234568/);
});

test('SDK connectivity alert tells the user when quotes fall back and when the SDK recovers', () => {
    assert.match(formatSdkConnectivityAlert(false, new Date('2026-01-01T12:00:00Z')), /using the UAPI fallback/);
    assert.match(formatSdkConnectivityAlert(true, new Date('2026-01-01T12:00:00Z')), /Primary quote source restored/);
    assert.match(formatQuoteFeedAlert(false, null, new Date('2026-01-01T12:00:00Z')), /Price monitoring is paused/);
});

test('total quote outage notifies once, UAPI recovery resumes monitoring, and SDK recovery is reported', async t => {
    const { dir, db } = fixture();
    t.after(() => { db.close(); fs.rmSync(dir, { recursive: true, force: true }); });
    let requests = 0;
    const fetchImpl = async () => { requests += 1; return { ok: true, json: async () => ({ ok: true, result: { message_id: requests } }) }; };
    const now = new Date('2026-01-01T12:00:00Z');
    const stock = { mapping: { verified_at: '2026-01-01T00:00:00Z', currency: 'EUR' } };
    const offlineRows = [{ stock, error: new Error('SDK and UAPI failed') }];
    const offline = await checkQuoteAvailability({ db, quoteRows: offlineRows, sdkEnabled: true, telegram: { token: 'token', chatId: '42' }, fetchImpl, now });
    const stillOffline = await checkQuoteAvailability({ db, quoteRows: offlineRows, sdkEnabled: true, telegram: { token: 'token', chatId: '42' }, fetchImpl, now });
    const uapiRecovery = await checkQuoteAvailability({ db, quoteRows: [{ stock, quote: { source: 'trade_republic_order_price' } }], sdkEnabled: true, telegram: { token: 'token', chatId: '42' }, fetchImpl, now });
    const sdkRecovery = await checkQuoteAvailability({ db, quoteRows: [{ stock, quote: { source: 'trade_republic_sdk_order_price' } }], sdkEnabled: true, telegram: { token: 'token', chatId: '42' }, fetchImpl, now });
    assert.equal(offline.notified, true);
    assert.equal(stillOffline.notified, false);
    assert.equal(uapiRecovery.notified, true);
    assert.equal(sdkRecovery.notified, true);
    assert.equal(requests, 3);
    assert.deepEqual(db.prepare("SELECT status,last_notified_status FROM broker_health_state WHERE broker='trade_republic_quotes'").get(), { status: 'ONLINE', last_notified_status: 'ONLINE' });
    assert.deepEqual(db.prepare("SELECT status,last_notified_status FROM broker_health_state WHERE broker='trade_republic_sdk'").get(), { status: 'ONLINE', last_notified_status: 'ONLINE' });
});

test('monitor stores SDK quote precision and evaluates BUY against ask and SELL against bid', async t => {
    const { dir, db } = fixture();
    t.after(() => { db.close(); fs.rmSync(dir, { recursive: true, force: true }); });
    const id = addMappedStock(db, { symbol: 'SIX', buy: 1.234568, sell: 1.234567 });
    db.prepare("INSERT INTO trades(trade_id,stock_id,opened_at,quantity,currency,entry_price,entry_fx_rate,entry_price_eur) VALUES ('SIX-LOT',?,'2026-01-01 00:00:00',1,'EUR',1,1,1)").run(id);
    db.prepare("INSERT INTO alert_levels(stock_id,side,price_eur) VALUES (?, 'BUY', 1.234568), (?, 'SELL', 1.234567)").run(id, id);
    let sidecarCalls = 0, telegramCalls = 0;
    const code = await runMonitor({
        db,
        now: new Date('2026-01-01T12:00:00Z'),
        config: {
            lockPath: path.join(dir, 'lock'), concurrency: 2, cycleDeadlineMs: 5000,
            tradeRepublicSdk: { isEnabled: () => true, getQuote: async () => ({ bid_eur: 1.234567, ask_eur: 1.234568, currency: 'EUR', bid_unit: 'EUR', ask_unit: 'EUR', observed_at: '2026-01-01T12:00:00Z' }), close() {} },
            tradeRepublic: { getQuote: async () => { sidecarCalls += 1; throw new Error('unused backup'); } },
            telegram: { token: 'token', chatId: '42' }
        },
        fetchImpl: async url => { if (url.includes('api.telegram.org')) { telegramCalls += 1; return { ok: true, json: async () => ({ ok: true, result: { message_id: telegramCalls } }) }; } throw new Error('unexpected network request'); },
        log() {}, error: assert.fail
    });

    assert.equal(code, EXIT.OK);
    assert.equal(sidecarCalls, 0);
    assert.equal(telegramCalls, 2);
    assert.equal(db.prepare('SELECT current_price_eur FROM stocks WHERE id=?').get(id).current_price_eur, 1.2345675);
    assert.deepEqual(db.prepare('SELECT current_bid_eur,current_ask_eur,current_midpoint_eur FROM stocks WHERE id=?').get(id), { current_bid_eur: 1.234567, current_ask_eur: 1.234568, current_midpoint_eur: 1.2345675 });
    assert.deepEqual(db.prepare('SELECT side,triggered FROM alert_levels ORDER BY side').all(), [{ side: 'BUY', triggered: 1 }, { side: 'SELL', triggered: 1 }]);
});

test('one stock quote failure does not stop other symbols in the cycle', async t => {
    const { dir, db } = fixture();
    t.after(() => { db.close(); fs.rmSync(dir, { recursive: true, force: true }); });
    const goodId = addMappedStock(db, { symbol: 'GOOD', buy: 1, sell: 2 });
    const badId = addMappedStock(db, { symbol: 'BAD', buy: 1, sell: 2 });
    const errors = [];
    const code = await runMonitor({
        db, now: new Date('2026-01-01T12:00:00Z'),
        config: {
            lockPath: path.join(dir, 'lock'), concurrency: 2, cycleDeadlineMs: 5000,
            tradeRepublicSdk: { isEnabled: () => false },
            tradeRepublic: { getQuote: async ({ isin }) => {
                if (isin.endsWith(String(badId).padStart(10, '0'))) throw new Error('provider unavailable');
                return { bid_eur: 10, ask_eur: 11, currency: 'EUR', bid_unit: 'EUR', ask_unit: 'EUR' };
            } }, telegram: {}
        }, log() {}, error: message => errors.push(message)
    });
    assert.equal(code, EXIT.PARTIAL);
    assert.equal(db.prepare('SELECT current_price_eur FROM stocks WHERE id=?').get(goodId).current_price_eur, 10.5);
    assert.equal(db.prepare('SELECT current_price_eur FROM stocks WHERE id=?').get(badId).current_price_eur, null);
    assert.match(errors.join('\n'), /BAD.*provider unavailable/);
});

test('Telegram request encodes its message and returns the delivery id', async () => {
    let request;
    const id = await sendTelegram('hello', { token: 'test-token', chatId: '42', fetchImpl: async (url, options) => {
        request = { url, options };
        return { ok: true, json: async () => ({ ok: true, result: { message_id: 7 } }) };
    } });
    assert.equal(id, 7);
    assert.match(request.url, /sendMessage$/);
    assert.match(request.options.body.toString(), /chat_id=42/);
});

test('single-run lock is exclusive and releasable', t => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-lock-'));
    t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
    const lock = path.join(dir, 'lock');
    const release = acquireLock(lock);
    assert.ok(release);
    assert.equal(acquireLock(lock), null);
    release();
    assert.ok(acquireLock(lock));
});
