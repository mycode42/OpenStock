'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const Database = require('better-sqlite3');
const { parseYahooQuote, parseTradeRepublicMonitorQuote, getExchangeRate, sendTelegram, formatAlert, checkTradeRepublicConnectivity, runMonitor, acquireLock, EXIT } = require('../lib/monitor');
const { initDatabase } = require('../database/init');

function temp() { return fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-monitor-')); }
function yahoo(price = 100, session = 'regular') { return { chart: { result: [{ meta: { shortName: 'Example', currentTradingPeriod: { [session]: { start: 1, end: 9999999999 } } }, timestamp: [10], indicators: { quote: [{ close: [price] }] } }] } }; }

test('Yahoo parser selects last close and session source', () => { const quote = parseYahooQuote(yahoo(123.45)); assert.equal(quote.priceUsd, 123.45); assert.equal(quote.source, 'yahoo_regular_close'); assert.equal(quote.name, 'Example'); });
test('Yahoo parser falls back to regular market price', () => { const quote = parseYahooQuote({ chart: { result: [{ meta: { regularMarketPrice: 9.5 } }] } }); assert.equal(quote.source, 'yahoo_regular_market_price'); });
test('quote validation rejects missing or non-positive prices', () => { assert.throws(() => parseYahooQuote({ chart: { result: [{ meta: { regularMarketPrice: 0 } }] } }), /Invalid Yahoo price/); });
test('Trade Republic monitor quote uses executable midpoint and preserves timestamp', () => { const quote = parseTradeRepublicMonitorQuote({ bid_eur: 10, ask_eur: 12, bid_unit: 'EUR', ask_unit: 'EUR', bid_time: '2026-01-01T12:00:00Z', currency: 'EUR' }); assert.equal(quote.priceEur, 11); assert.equal(quote.source, 'trade_republic_order_price'); assert.equal(quote.quoteTime, '2026-01-01T12:00:00.000Z'); });
test('FX uses fresh cache and stale fallback without network in those paths', async () => { const dir = temp(); const file = path.join(dir, 'fx.json'); fs.writeFileSync(file, JSON.stringify({ rate: 0.91, timestamp: Math.floor(Date.now() / 1000) })); const cached = await getExchangeRate({ cachePath: file, ttlMs: 3600000, url: 'mock', fetchImpl: () => { throw new Error('network used'); } }); assert.equal(cached.rate, 0.91); const stale = await getExchangeRate({ cachePath: file, ttlMs: 0, url: 'mock', fetchImpl: async () => { throw new Error('offline'); } }); assert.equal(stale.source, 'stale-cache'); });
test('Telegram posts encoded message and returns message id', async () => { let request; const id = await sendTelegram('hello', { token: 'test-token', chatId: '42', fetchImpl: async (url, options) => { request = { url, options }; return { ok: true, json: async () => ({ ok: true, result: { message_id: 7 } }) }; } }); assert.equal(id, 7); assert.match(request.url, /sendMessage$/); assert.match(request.options.body.toString(), /chat_id=42/); });
test('Telegram preview line starts with side, stock name, symbol, and target', () => { const message = formatAlert({ symbol: 'TEST', name: 'A&B', buy_threshold_eur: 1, sell_threshold_eur: 2 }, 'SELL_TRIGGERED', 2.5, 0.9, new Date('2026-01-01T12:00:00Z')); assert.equal(message.split('\n')[0], '<b>🔽 SELL · A&amp;B (TEST) · €2.00</b>'); assert.match(message, /Current price: €2\.50/); assert.match(message, /UTC/); });

test('Trade Republic connectivity alerts once per transition and records recovery', async t => {
    const dir = temp(); const file = path.join(dir, 'test.db');
    t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
    initDatabase({ dbPath: file, seedDefaults: false, silent: true });
    const db = new Database(file);
    t.after(() => db.close());
    let status = 'ok'; const messages = [];
    const tradeRepublic = { health: async () => status === 'ok' ? { status: 'ok' } : { status: 'error', code: 'UNAVAILABLE' } };
    const fetchImpl = async (url, options) => { messages.push(new URLSearchParams(options.body).get('text')); return { ok: true, json: async () => ({ ok: true, result: { message_id: messages.length } }) }; };
    const telegram = { token: 'token', chatId: '42' };

    let result = await checkTradeRepublicConnectivity({ db, tradeRepublic, telegram, fetchImpl, now: new Date('2026-09-16T12:00:00Z') });
    assert.deepEqual({ online: result.online, notified: result.notified }, { online: true, notified: false });
    assert.equal(messages.length, 0);

    status = 'error';
    result = await checkTradeRepublicConnectivity({ db, tradeRepublic, telegram, fetchImpl, now: new Date('2026-09-16T12:01:00Z') });
    assert.deepEqual({ online: result.online, notified: result.notified }, { online: false, notified: true });
    assert.match(messages[0], /sidecar offline/);
    await checkTradeRepublicConnectivity({ db, tradeRepublic, telegram, fetchImpl, now: new Date('2026-09-16T12:02:00Z') });
    assert.equal(messages.length, 1);

    status = 'ok';
    result = await checkTradeRepublicConnectivity({ db, tradeRepublic, telegram, fetchImpl, now: new Date('2026-09-16T12:03:00Z') });
    assert.deepEqual({ online: result.online, notified: result.notified }, { online: true, notified: true });
    assert.match(messages[1], /sidecar online/);
    assert.deepEqual(db.prepare("SELECT status,last_notified_status,last_error FROM broker_health_state WHERE broker='trade_republic'").get(), { status: 'ONLINE', last_notified_status: 'ONLINE', last_error: null });
});
test('single-run lock is exclusive, releasable, and recovers a dead owner', () => { const dir = temp(); const lock = path.join(dir, 'lock'); const release = acquireLock(lock); assert.ok(release); assert.equal(acquireLock(lock), null); release(); fs.writeFileSync(lock, JSON.stringify({ pid: 2147483647, createdAt: 0 })); const recovered = acquireLock(lock); assert.ok(recovered); recovered(); });
test('monitor requires migrated schema and does not create alert levels', async () => { const dir = temp(); const db = new Database(path.join(dir, 'test.db')); db.exec(fs.readFileSync(path.join(__dirname, '..', 'database/schema.sql'), 'utf8')); await assert.rejects(() => runMonitor({ db, now: new Date('2026-01-01T12:00:00Z'), config: { lockPath: path.join(dir, 'lock'), timeoutMs: 100, yahooUrl: 'mock', fx: { cachePath: path.join(dir, 'missing-fx.json'), ttlMs: 1000, url: 'fx' }, telegram: {} }, fetchImpl: async () => ({ ok: true, json: async () => ({ rates: { EUR: 1 } }) }) }), /Migrated alert-level schema/); db.close(); });
test('monitor writes prepared quote/history, isolates failed stocks, and alerts independent levels', async () => { const dir = temp(); const file = path.join(dir, 'test.db'); initDatabase({ dbPath: file, seedDefaults: false, silent: true }); const db = new Database(file); db.prepare(`INSERT INTO stocks(symbol,name,buy_threshold_eur,sell_threshold_eur,owned) VALUES ('BAD','Bad',1,2,0),('GOOD','Good',100,200,0)`).run(); const stockId = db.prepare("SELECT id FROM stocks WHERE symbol='GOOD'").get().id; db.prepare("INSERT INTO alert_levels(stock_id,side,price_eur) VALUES (?, 'BUY', 100), (?, 'BUY', 95), (?, 'SELL', 80)").run(stockId, stockId, stockId); const calls = []; const code = await runMonitor({ db, now: new Date('2026-01-01T12:00:00Z'), config: { lockPath: path.join(dir, 'lock'), timeoutMs: 100, yahooUrl: 'mock', fx: { cachePath: path.join(dir, 'fx.json'), ttlMs: 999999, url: 'fx' }, telegram: { token: 'token', chatId: '42' } }, fetchImpl: async url => { calls.push(url); if (url === 'fx') return { ok: true, json: async () => ({ rates: { EUR: 1 } }) }; if (url.includes('/BAD?')) throw new Error('bad stock'); if (url.includes('api.telegram.org')) return { ok: true, json: async () => ({ ok: true, result: { message_id: 1 } }) }; return { ok: true, json: async () => yahoo(90) }; }, log: () => {}, error: () => {} }); assert.equal(code, EXIT.PARTIAL, 'exit'); assert.equal(db.prepare("SELECT current_price_eur FROM stocks WHERE symbol='GOOD'").get().current_price_eur, 90, 'price'); assert.equal(db.prepare('SELECT COUNT(*) count FROM price_history').get().count, 1, 'history'); assert.equal(db.prepare('SELECT COUNT(*) count FROM alerts_log').get().count, 2, 'two BUY levels'); assert.equal(db.prepare('SELECT COUNT(*) count FROM alert_levels WHERE triggered=1').get().count, 2, 'retired after delivery'); assert.equal(calls.length, 5, 'calls'); db.close(); });

test('monitor uses Trade Republic midpoint in EUR and stores broker source/timestamp', async () => {
    const dir = temp(); const file = path.join(dir, 'test.db'); initDatabase({ dbPath: file, seedDefaults: false, silent: true }); const db = new Database(file);
    db.prepare("INSERT INTO stocks(symbol,name,buy_threshold_eur,sell_threshold_eur,price_source) VALUES ('TR','Trade',1,20,'trade_republic')").run();
    const stockId = db.prepare("SELECT id FROM stocks WHERE symbol='TR'").get().id;
    db.prepare("INSERT INTO broker_instrument_mappings(stock_id,broker,ticker,isin,exchange,currency,verified_at) VALUES (?, 'trade_republic','TR','US0000000001','TIB','EUR','2026-01-01T00:00:00Z')").run(stockId);
    let brokerCalls = 0; const logs = [];
    const code = await runMonitor({ db, now: new Date('2026-01-01T12:00:00Z'), config: { lockPath: path.join(dir, 'lock'), timeoutMs: 100, yahooUrl: 'mock', fx: { cachePath: path.join(dir, 'fx.json'), ttlMs: 999999, url: 'fx' }, telegram: {}, tradeRepublic: { getQuote: async request => { brokerCalls += 1; assert.deepEqual(request, { isin: 'US0000000001', exchange: 'TIB' }); return { bid_eur: 10, ask_eur: 12, bid_unit: 'EUR', ask_unit: 'EUR', currency: 'EUR', bid_time: '2026-01-01T11:59:00Z' }; } } }, fetchImpl: async url => { assert.equal(url, 'fx'); return { ok: true, json: async () => ({ rates: { EUR: 0.5 } }) }; }, log: message => logs.push(message), error: () => {} });
    assert.equal(code, EXIT.OK); assert.equal(brokerCalls, 1); assert.equal(db.prepare("SELECT current_price_eur FROM stocks WHERE id=?").get(stockId).current_price_eur, 11); assert.equal(db.prepare("SELECT price_usd,price_eur,source,quote_time FROM price_history WHERE stock_id=?").get(stockId).price_usd, null); assert.equal(db.prepare("SELECT price_eur FROM price_history WHERE stock_id=?").get(stockId).price_eur, 11); assert.equal(db.prepare("SELECT source,quote_time FROM price_history WHERE stock_id=?").get(stockId).source, 'trade_republic_order_price'); assert.match(db.prepare("SELECT quote_time FROM price_history WHERE stock_id=?").get(stockId).quote_time, /^2026-01-01 11:59:00/); assert.equal(logs.some(line => line.includes('Trade Republic price fallback')), false); db.close();
});

test('monitor falls back to Yahoo when Trade Republic mapping is missing', async () => {
    const dir = temp(); const file = path.join(dir, 'test.db'); initDatabase({ dbPath: file, seedDefaults: false, silent: true }); const db = new Database(file);
    db.prepare("INSERT INTO stocks(symbol,name,buy_threshold_eur,sell_threshold_eur,price_source) VALUES ('TRMISS','Missing',1,20,'trade_republic')").run(); let brokerCalls = 0; let yahooCalls = 0; const logs = [];
    const code = await runMonitor({ db, now: new Date('2026-01-01T12:00:00Z'), config: { lockPath: path.join(dir, 'lock'), timeoutMs: 100, yahooUrl: 'mock', fx: { cachePath: path.join(dir, 'fx.json'), ttlMs: 999999, url: 'fx' }, telegram: {}, tradeRepublic: { getQuote: async () => { brokerCalls += 1; throw new Error('must not call without mapping'); } } }, fetchImpl: async url => { if (url === 'fx') return { ok: true, json: async () => ({ rates: { EUR: 1 } }) }; yahooCalls += 1; return { ok: true, json: async () => yahoo(7) }; }, log: message => logs.push(message), error: () => {} });
    assert.equal(code, EXIT.OK); assert.equal(brokerCalls, 0); assert.equal(yahooCalls, 1); assert.equal(db.prepare("SELECT source FROM price_history WHERE stock_id=1").get().source, 'yahoo_regular_close'); assert.match(logs.join('\n'), /Trade Republic sidecar quote unavailable \(verified Trade Republic mapping unavailable\)/); db.close();
});

test('monitor falls back to Yahoo when Trade Republic quote fails', async () => {
    const dir = temp(); const file = path.join(dir, 'test.db'); initDatabase({ dbPath: file, seedDefaults: false, silent: true }); const db = new Database(file);
    db.prepare("INSERT INTO stocks(symbol,name,buy_threshold_eur,sell_threshold_eur,price_source,current_price_eur) VALUES ('TRFAIL','Failure',91,120,'trade_republic',77)").run(); const stockId = db.prepare("SELECT id FROM stocks WHERE symbol='TRFAIL'").get().id; db.prepare("INSERT INTO broker_instrument_mappings(stock_id,broker,ticker,isin,exchange,currency,verified_at) VALUES (?, 'trade_republic','TRFAIL','US0000000002','TIB','EUR','2026-01-01T00:00:00Z')").run(stockId); db.prepare("INSERT INTO alert_levels(stock_id,side,price_eur) VALUES (?, 'BUY', 91)").run(stockId); let yahooCalls = 0; const logs = []; const telegram = [];
    const code = await runMonitor({ db, now: new Date('2026-01-01T12:00:00Z'), config: { lockPath: path.join(dir, 'lock'), timeoutMs: 100, yahooUrl: 'mock', fx: { cachePath: path.join(dir, 'fx.json'), ttlMs: 999999, url: 'fx' }, telegram: { token: 'token', chatId: '42' }, tradeRepublic: { getQuote: async () => { throw new Error('sidecar unavailable'); } } }, fetchImpl: async (url, options) => { if (url === 'fx') return { ok: true, json: async () => ({ rates: { EUR: 0.9 } }) }; if (url.includes('api.telegram.org')) { telegram.push(new URLSearchParams(options.body).get('text')); return { ok: true, json: async () => ({ ok: true, result: { message_id: 9 } }) }; } yahooCalls += 1; return { ok: true, json: async () => yahoo(100) }; }, log: message => logs.push(message), error: assert.fail });
    const observation = db.prepare("SELECT price_usd,price_eur,exchange_rate,source FROM price_history WHERE stock_id=?").get(stockId);
    assert.equal(code, EXIT.OK); assert.equal(yahooCalls, 1); assert.deepEqual(observation, { price_usd: 100, price_eur: 90, exchange_rate: 0.9, source: 'yahoo_regular_close' }); assert.equal(db.prepare("SELECT current_price_eur FROM stocks WHERE id=?").get(stockId).current_price_eur, 90); assert.equal(db.prepare("SELECT COUNT(*) count FROM alerts_log WHERE stock_id=?").get(stockId).count, 1); assert.match(telegram[0], /Current price: €90\.00/); assert.match(logs.join('\n'), /Trade Republic sidecar quote unavailable \(sidecar unavailable\)/); db.close();
});

test('sell is suppressed without a resolved position and Telegram failure does not retire a level', async () => { const dir = temp(); const file = path.join(dir, 'test.db'); initDatabase({ dbPath: file, seedDefaults: false, silent: true }); const db = new Database(file); db.prepare("INSERT INTO stocks(symbol,name,buy_threshold_eur,sell_threshold_eur,owned) VALUES ('NONE','None',1,80,0),('HELD','Held',1,80,1)").run(); const none = db.prepare("SELECT id FROM stocks WHERE symbol='NONE'").get().id; const held = db.prepare("SELECT id FROM stocks WHERE symbol='HELD'").get().id; db.prepare("INSERT INTO trades(trade_id,stock_id,opened_at,quantity,currency,entry_price,entry_fx_rate,entry_price_eur) VALUES ('HELD-1',?,'2026-01-01 00:00:00',1,'EUR',50,1,50)").run(held); db.prepare("INSERT INTO alert_levels(stock_id,side,price_eur) VALUES (?, 'SELL', 80), (?, 'SELL', 80)").run(none, held); const code = await runMonitor({ db, now: new Date('2026-01-01T12:00:00Z'), config: { lockPath: path.join(dir, 'lock'), timeoutMs: 100, yahooUrl: 'mock', fx: { cachePath: path.join(dir, 'fx.json'), ttlMs: 999999, url: 'fx' }, telegram: { token: 'token', chatId: '42' } }, fetchImpl: async url => { if (url === 'fx') return { ok: true, json: async () => ({ rates: { EUR: 1 } }) }; if (url.includes('api.telegram.org')) return { ok: true, json: async () => ({ ok: false }) }; return { ok: true, json: async () => yahoo(90) }; }, log: () => {}, error: () => {} }); assert.equal(code, EXIT.PARTIAL); assert.equal(db.prepare('SELECT COUNT(*) count FROM alerts_log').get().count, 0); assert.equal(db.prepare('SELECT COUNT(*) count FROM alert_levels WHERE triggered=1').get().count, 0); db.close(); });

test('Trade Republic success skips FX and compares BUY to ask and SELL to bid', async () => {
    const dir = temp(); const file = path.join(dir, 'test.db'); initDatabase({ dbPath: file, seedDefaults: false, silent: true }); const db = new Database(file);
    db.prepare("INSERT INTO stocks(symbol,name,buy_threshold_eur,sell_threshold_eur,price_source) VALUES ('BUY','Buy',11.5,20,'trade_republic'),('SELL','Sell',1,10.5,'trade_republic')").run();
    const buy = db.prepare("SELECT id FROM stocks WHERE symbol='BUY'").get().id; const sell = db.prepare("SELECT id FROM stocks WHERE symbol='SELL'").get().id;
    db.prepare("INSERT INTO trades(trade_id,stock_id,opened_at,quantity,currency,entry_price,entry_fx_rate,entry_price_eur) VALUES ('SELL-1',?,'2026-01-01 00:00:00',1,'EUR',5,1,5)").run(sell);
    db.prepare("INSERT INTO broker_instrument_mappings(stock_id,broker,ticker,isin,exchange,currency,verified_at) VALUES (?, 'trade_republic','BUY','US0000000001','TIB','EUR','2026-01-01T00:00:00Z'), (?, 'trade_republic','SELL','US0000000002','TIB','EUR','2026-01-01T00:00:00Z')").run(buy, sell);
    db.prepare("INSERT INTO alert_levels(stock_id,side,price_eur) VALUES (?, 'BUY', 11.5), (?, 'SELL', 10.5)").run(buy, sell);
    let fxCalls = 0;
    const code = await runMonitor({ db, now: new Date('2026-01-01T12:00:00Z'), config: { lockPath: path.join(dir, 'lock'), timeoutMs: 100, yahooUrl: 'mock', fx: { cachePath: path.join(dir, 'fx.json'), ttlMs: 999999, url: 'fx' }, telegram: {}, tradeRepublic: { getQuote: async ({ isin }) => ({ bid_eur: 10, ask_eur: 12, currency: 'EUR', bid_unit: 'EUR', ask_unit: 'EUR', quote_time: '2026-01-01T11:59:00Z', source: 'test' }) } }, fetchImpl: async url => { if (url === 'fx') fxCalls++; throw new Error('Yahoo/FX must not be called'); }, log: () => {}, error: assert.fail });
    assert.equal(code, EXIT.OK); assert.equal(fxCalls, 0); assert.equal(db.prepare('SELECT COUNT(*) count FROM alerts_log').get().count, 0); db.close();
});
