const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { initDatabase } = require('../database/init');
const OpenStockDB = require('../database/db');
const { createApp, berlinTradingDayStart, berlinTradingDayEnd } = require('../server');
const APP_VERSION = require('../package.json').version;

async function fixture({ now } = {}) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-api-'));
    const file = path.join(dir, 'test.db');
    initDatabase({ dbPath: file, seedDefaults: false, silent: true });
    const db = new OpenStockDB(file);
    const backfill = { enqueue() {}, enqueueMissing() { return 0; } };
    const fetchImpl = async () => { throw new Error('unexpected external fetch'); };
    const app = createApp({ db, backfill, fetchImpl, now, sdkClient: { isEnabled: () => false } });
    app.locals.brokerClient = {
        getQuote: async ({ isin }) => {
            const symbol = Buffer.from(isin.slice(2), 'hex').toString('ascii').replace(/\0/g, '');
            if (symbol === 'BAD') throw new Error('mock quote failure');
            return { bid_eur: 89.999999, ask_eur: 90.000001, currency: 'EUR', bid_unit: 'EUR', ask_unit: 'EUR', observed_at: '2026-09-17T12:00:00Z' };
        },
        getInstrument: async isin => { const decoded = Buffer.from(isin.slice(2), 'hex').toString('ascii').replace(/\0/g, ''); const symbol = /^[A-Z0-9.-]+$/.test(decoded) && decoded ? decoded : `M${isin.slice(-5)}`; return { isin, name: `Mock ${symbol}`, shortName: `Mock ${symbol}`, intlSymbol: symbol, tradable: true, listings: [{ exchange: 'XNAS', symbolAtExchange: symbol }] }; },
        getTradeRepublicInstrument: async isin => ({ isin, exchange: 'TIB', currency: 'EUR', tradable: true }),
        getInstrumentHistory: async () => ({ points: [] })
    };
    const server = await new Promise(resolve => { const listener = app.listen(0, '127.0.0.1', () => resolve(listener)); });
    const base = `http://127.0.0.1:${server.address().port}`;
    const request = (url, options = {}) => {
        if (url === '/api/stocks' && options.method === 'POST' && options.body) {
            const body = JSON.parse(options.body);
            if (!body.isin && body.symbol) body.isin = `US${Buffer.from(body.symbol).toString('hex').toUpperCase().padEnd(10, '0').slice(0, 10)}`;
            options = { ...options, body: JSON.stringify(body) };
        }
        return fetch(base + url, options);
    };
    return { app, db, dir, server, request };
}

function addAlpacaStock(fixture, stock) {
    const result = fixture.db.db.prepare(`INSERT INTO alpaca_watchlist (symbol, name, source_stock_id, copied_fx_rate)
        VALUES (?, ?, ?, 1)`).run(stock.symbol, stock.name, stock.id);
    return Number(result.lastInsertRowid);
}

test('Berlin trading day starts at 07:30 with daylight-saving offsets', () => {
    assert.equal(berlinTradingDayStart(new Date('2026-09-16T15:45:00Z')).toISOString(), '2026-09-16T05:30:00.000Z');
    assert.equal(berlinTradingDayStart(new Date('2026-12-16T12:00:00Z')).toISOString(), '2026-12-16T06:30:00.000Z');
    assert.equal(berlinTradingDayStart(new Date('2026-09-16T04:00:00Z')).toISOString(), '2026-09-15T05:30:00.000Z');
});

test('Berlin one-day chart ends at 22:00 local time', () => {
    assert.equal(berlinTradingDayEnd(new Date('2026-09-16T15:45:00Z')).toISOString(), '2026-09-16T20:00:00.000Z');
    assert.equal(berlinTradingDayEnd(new Date('2026-12-16T12:00:00Z')).toISOString(), '2026-12-16T21:00:00.000Z');
});

test('mutation origin check accepts the public HTTPS origin forwarded by the reverse proxy', async t => {
    const f = await fixture();
    t.after(() => { f.server.close(); f.db.db.close(); fs.rmSync(f.dir, { recursive: true, force: true }); });
    const headers = {
        'content-type': 'application/json',
        origin: 'https://openstock.example.test',
        host: 'openstock.example.test',
        'x-forwarded-proto': 'https',
        'x-forwarded-host': 'openstock.example.test'
    };
    const accepted = await f.request('/api/stocks', { method: 'POST', headers, body: JSON.stringify({ symbol: 'PROXY', name: 'Proxy', buy_threshold_eur: 1, sell_threshold_eur: 2 }) });
    assert.equal(accepted.status, 201);

    const rejected = await f.request('/api/stocks', { method: 'POST', headers: { ...headers, origin: 'https://other.example.test' }, body: JSON.stringify({ symbol: 'CROSS', name: 'Cross', buy_threshold_eur: 1, sell_threshold_eur: 2 }) });
    assert.equal(rejected.status, 403);
    assert.equal((await rejected.json()).error, 'Same-origin mutation required');
});

test('analytics reads do not create equity snapshots', async t => {
    const f = await fixture();
    t.after(() => { f.server.close(); f.db.db.close(); fs.rmSync(f.dir, { recursive: true, force: true }); });
    const before = f.db.db.prepare('SELECT COUNT(*) AS count FROM equity_snapshots').get().count;
    const response = await f.request('/api/analytics/dashboard');
    assert.equal(response.status, 200);
    const after = f.db.db.prepare('SELECT COUNT(*) AS count FROM equity_snapshots').get().count;
    assert.equal(after, before);
});

test('broker confirmed journal executions are read-only through the API', async t => {
    const f = await fixture();
    t.after(() => { f.server.close(); f.db.db.close(); fs.rmSync(f.dir, { recursive: true, force: true }); });
    const stock = f.db.addStock('LOCKED', 'Locked', 'EUR', 1, 2);
    const inserted = f.db.addTrade({
        trade_id: 'TR-EXEC-read-only', stock_id: Number(stock.lastInsertRowid), opened_at: '2026-09-17 10:00:00', closed_at: null,
        quantity: 1, currency: 'EUR', entry_price: 10, entry_fx_rate: 1, entry_price_eur: 10, entry_fee_eur: 0,
        exit_price: null, exit_fx_rate: null, exit_price_eur: null, exit_fee_eur: 0, dividend_eur: 0, strategy: 'trade_republic_execution', notes: ''
    });
    const tradeId = Number(inserted.lastInsertRowid);
    assert.equal(f.db.getTradeById(tradeId).broker_confirmed, 1);
    const edit = await f.request(`/api/trades/${tradeId}`, { method: 'PUT', headers: { 'content-type': 'application/json' }, body: '{}' });
    const close = await f.request(`/api/trades/${tradeId}/close`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' });
    assert.equal(edit.status, 409);
    assert.equal(close.status, 409);
    assert.equal(f.db.getTradeById(tradeId).quantity, 1);
});

test('percentage alert API captures the latest saved EUR quote as its reference', async t => {
    const f = await fixture();
    t.after(() => { f.server.close(); f.db.db.close(); fs.rmSync(f.dir, { recursive: true, force: true }); });
    const stock = f.db.addStock('PCT', 'Percent', 'EUR', 80, 120);
    f.db.db.prepare('UPDATE stocks SET current_price_eur=100 WHERE id=?').run(stock.lastInsertRowid);
    const response = await f.request(`/api/stocks/${stock.lastInsertRowid}/percent-alerts`, {
        method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ direction: 'DROP', change_pct: 5 })
    });
    const result = await response.json();
    assert.equal(response.status, 201, JSON.stringify(result));
    const { alert_level: level } = result;
    assert.deepEqual([level.trigger_mode, level.side, level.reference_price_eur, level.price_eur], ['PERCENT', 'BUY', 100, 95]);
});

test('live prices use Alpaca quotes in memory only and coalesce concurrent requests', async t => {
    const f = await fixture({ now: () => new Date('2026-09-17T12:00:00Z') });
    t.after(() => { f.server.close(); f.db.db.close(); fs.rmSync(f.dir, { recursive: true, force: true }); });
    const created = await f.request('/api/stocks', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ symbol: 'LIVE', name: 'Live', buy_threshold_eur: 1, sell_threshold_eur: 2 }) });
    const stock = (await created.json()).stock;
    const alpacaStockId = addAlpacaStock(f, stock);
    let calls = 0;
    f.app.locals.alpacaClient = { getLatestQuotes: async () => { calls += 1; return { feed: 'iex', quotes: { LIVE: { bp: 12.345678, ap: 12.345679, t: '2026-09-17T12:00:00.000Z' } } }; } };
    const [a, b] = await Promise.all([f.request('/api/live-prices'), f.request('/api/live-prices')]);
    const payload = await a.json();
    assert.equal(b.status, 200);
    assert.equal(calls, 1);
    assert.deepEqual(payload.prices[0], { stock_id: alpacaStockId, symbol: 'LIVE', currency: 'USD', price_usd: 12.3456785, bid_usd: 12.345678, ask_usd: 12.345679, quote_time: '2026-09-17T12:00:00.000Z', feed: 'iex', source: 'alpaca_iex_quote' });
    assert.equal(f.db.getStockById(stock.id).current_price_eur, null);
    assert.equal(f.db.db.prepare('SELECT COUNT(*) AS count FROM price_history WHERE stock_id=?').get(stock.id).count, 0);
});

test('live prices label the Alpaca feed returned by the provider', async t => {
    const f = await fixture({ now: () => new Date('2026-10-02T11:00:00Z') });
    t.after(() => { f.server.close(); f.db.db.close(); fs.rmSync(f.dir, { recursive: true, force: true }); });
    const created = await f.request('/api/stocks', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ symbol: 'SDK', name: 'SDK', buy_threshold_eur: 1, sell_threshold_eur: 2 }) });
    const stock = (await created.json()).stock;
    const alpacaStockId = addAlpacaStock(f, stock);
    let calls = 0;
    f.app.locals.alpacaClient = { getLatestQuotes: async () => { calls += 1; return { feed: 'sip', quotes: { SDK: { bp: 10.123456, ap: 10.123457, t: '2026-10-02T11:00:00.000Z' } } }; } };
    const payload = await (await f.request('/api/live-prices')).json();
    assert.equal(calls, 1);
    assert.deepEqual(payload.prices[0], { stock_id: alpacaStockId, symbol: 'SDK', currency: 'USD', price_usd: 10.1234565, bid_usd: 10.123456, ask_usd: 10.123457, quote_time: '2026-10-02T11:00:00.000Z', feed: 'sip', source: 'alpaca_sip_quote' });
    assert.equal(f.db.getStockById(stock.id).current_price_eur, null);
    assert.equal(f.db.db.prepare('SELECT COUNT(*) AS count FROM price_history WHERE stock_id=?').get(stock.id).count, 0);
});

test('one-day history starts at 07:30 Berlin time and uses fifteen-minute buckets', async t => {
    const chartNow = new Date();
    const tradingStart = berlinTradingDayStart(chartNow);
    const f = await fixture({ now: () => chartNow });
    t.after(() => { f.server.close(); f.db.db.close(); fs.rmSync(f.dir, { recursive: true, force: true }); });
    const response = await f.request('/api/stocks', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ symbol: 'DAY', name: 'Trading day', buy_threshold_eur: 80, sell_threshold_eur: 120 }) });
    const stock = (await response.json()).stock;
    f.db.addPriceSnapshot(stock.id, null, 99, null, 'trade_republic_history', new Date(tradingStart.getTime() - 600000).toISOString(), new Date(tradingStart.getTime() - 600000).toISOString().replace('T', ' ').replace('.000Z', ''));
    for (let minute = 0; minute < 60; minute += 1) {
        const timestamp = new Date(tradingStart.getTime() + minute * 60000).toISOString();
        f.db.addPriceSnapshot(stock.id, null, 100 + minute, null, 'trade_republic_history', timestamp, timestamp.replace('T', ' ').replace('.000Z', ''));
    }
    const sdkTimestamp = new Date(tradingStart.getTime() + 60 * 60000).toISOString();
    f.db.addPriceSnapshot(stock.id, null, 160, null, 'trade_republic_sdk_order_price', sdkTimestamp, sdkTimestamp.replace('T', ' ').replace('.000Z', ''));
    const history = await (await f.request(`/api/stocks/${stock.id}/history?range=1d`)).json();
    assert.equal(history.start_at, tradingStart.toISOString());
    assert.equal(history.end_at, new Date(tradingStart.getTime() + (14 * 60 + 30) * 60000).toISOString());
    assert.equal(history.bucket_seconds, 900);
    assert.equal(history.points.length, 48);
    assert.equal(history.points[0].recorded_at, tradingStart.toISOString());
    assert.equal(history.points.at(-1).recorded_at, sdkTimestamp);
    assert.equal(history.points[0].price_eur, 100);
    assert.equal(history.points.at(-1).price_eur, 160);
    assert.equal(history.points.at(-1).source, 'trade_republic_sdk_order_price');

    const weekly = await (await f.request(`/api/stocks/${stock.id}/history?range=1w`)).json();
    assert.equal(weekly.range, '1w');
    assert.equal(weekly.bucket_seconds, 3600);
    assert.equal(weekly.max_gap_seconds, 21600);
    assert.equal(weekly.points.length, 2);
    assert.equal(weekly.points.at(-1).price_eur, 160);
});

test('verified-instrument charts exclude observations from legacy providers', async t => {
    const f = await fixture();
    t.after(() => { f.server.close(); f.db.db.close(); fs.rmSync(f.dir, { recursive: true, force: true }); });
    const response = await f.request('/api/stocks', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ symbol: 'GAP', name: 'Gap', buy_threshold_eur: 1, sell_threshold_eur: 2 }) });
    const stock = (await response.json()).stock;
    f.db.saveBrokerMapping({ stock_id: stock.id, broker: 'trade_republic', ticker: 'GAP', isin: stock.isin, exchange: 'TIB', currency: 'EUR', verified_at: '2026-09-01T00:00:00Z', source: 'test', broker_metadata_json: '{}' });
    const timestampAt = offsetMs => new Date(Date.now() + offsetMs).toISOString();
    for (const timestamp of [-5, -4, -3, -2].map(days => timestampAt(days * 24 * 60 * 60 * 1000))) {
        f.db.addPriceSnapshot(stock.id, null, 100, null, 'legacy_import', timestamp, timestamp.replace('T', ' ').replace('.000Z', ''));
    }
    const legacyTimestamp = timestampAt(-2 * 60 * 60 * 1000);
    const verifiedTimestamp = timestampAt(-60 * 60 * 1000);
    f.db.addPriceSnapshot(stock.id, null, 101, null, 'legacy_import', legacyTimestamp, legacyTimestamp.replace('T', ' ').replace('.000Z', ''));
    f.db.addPriceSnapshot(stock.id, null, 102, null, 'trade_republic_order_price', verifiedTimestamp, verifiedTimestamp.replace('T', ' ').replace('.000Z', ''));
    const history = await (await f.request(`/api/stocks/${stock.id}/history?range=1w`)).json();
    assert.deepEqual(history.points.map(point => [point.recorded_at, point.price_eur]), [[verifiedTimestamp, 102]]);
});

test('API factory covers lifecycle, history, journal, analytics, refresh isolation, and archive restore', async t => {
    const f = await fixture();
    t.after(() => { f.server.close(); f.db.db.close(); fs.rmSync(f.dir, { recursive: true, force: true }); });
    let response = await f.request('/health');
    assert.equal(response.status, 200);
    assert.match(response.headers.get('cache-control'), /no-store/);
    assert.equal(response.headers.get('x-openstock-version'), APP_VERSION);
    assert.equal((await response.json()).version, APP_VERSION);

    response = await f.request('/api/stocks', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ symbol: 'GOOD', name: 'Good <Stock>', buy_threshold_eur: 80, sell_threshold_eur: 120 }) });
    assert.equal(response.status, 201);
    const good = (await response.json()).stock;
    f.db.addPriceSnapshot(good.id, 100, 90, 0.9, 'legacy_import', null, '2026-01-01 12:00:00');
    f.db.addPriceSnapshot(good.id, null, 91, null, 'trade_republic_history', '2026-01-01T00:00:00.000Z', '2026-01-01 00:00:00');
    response = await f.request(`/api/stocks/${good.id}/history?range=1y`);
    const history = await response.json();
    assert.equal(history.currency, 'EUR');
    assert.equal(history.native_eur, true);
    assert.equal(history.source, 'trade_republic');
    assert.equal(history.points.length, 1);
    assert.equal(history.points[0].price_eur, 91);
    assert.equal(history.points[0].price_usd, null);
    assert.equal(history.points[0].source, 'trade_republic_history');
    assert.equal(history.points[0].recorded_at, '2026-01-01T00:00:00.000Z');
    assert.equal(history.points[0].quote_time, '2026-01-01T00:00:00.000Z');
    assert.equal(history.bucket_seconds, 345600);
    assert.equal(history.max_gap_seconds, 172800);
    assert.equal((await f.request('/api/stocks?includeArchived=1')).status, 200);

    await f.request('/api/capital-events', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ event_type: 'INITIAL', amount_eur: 10000, occurred_at: '2026-01-01T00:00:00Z', notes: 'seed' }) });
    response = await f.request('/api/trades', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ trade_id: 'T-1', stock_id: good.id, opened_at: '2026-01-02T00:00:00Z', quantity: 2, currency: 'EUR', entry_price: 90, entry_fx_rate: 1, entry_fee_eur: 1 }) });
    assert.equal(response.status, 201);
    const trade = (await response.json()).trade;
    assert.equal(trade.trade_number, 1);
    response = await f.request(`/api/trades/${trade.id}/close`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ closed_at: '2026-01-03T00:00:00Z', exit_price: 100, exit_fx_rate: 1, dividend_eur: 2 }) });
    assert.equal(response.status, 200);
    assert.equal((await response.json()).trade.exit_fee_eur, 1);
    response = await f.request('/api/trades', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ trade_id: 'T-default-fee', stock_id: good.id, opened_at: '2026-01-04T00:00:00Z', quantity: 1, currency: 'EUR', entry_price: 90, entry_fx_rate: 1 }) });
    assert.equal(response.status, 201);
    const secondTrade = (await response.json()).trade;
    assert.equal(secondTrade.entry_fee_eur, 1);
    assert.equal(secondTrade.trade_number, 2);
    assert.equal((await f.request('/api/analytics/dashboard')).status, 200);

    addAlpacaStock(f, good);
    f.app.locals.alpacaClient = { getLatestQuotes: async () => ({ feed: 'iex', quotes: { GOOD: { bp: 89.99, ap: 90.01, t: '2026-01-01T12:00:00.000Z' } } }) };
    const bad = await f.request('/api/stocks', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ symbol: 'BAD', name: 'Bad', buy_threshold_eur: 1, sell_threshold_eur: 2 }) });
    assert.equal(bad.status, 201);
    response = await f.request('/api/refresh-prices', { method: 'POST' });
    const refreshed = await response.json();
    assert.equal(response.status, 200);
    assert.equal(refreshed.updated, 1);
    assert.equal(refreshed.prices[0].symbol, 'GOOD');
    assert.equal(refreshed.prices[0].currency, 'USD');
    assert.equal(refreshed.prices[0].price_usd, 90);

    response = await f.request(`/api/stocks/${good.id}`, { method: 'DELETE' });
    assert.equal((await response.json()).archived, true);
    assert.equal((await f.request('/api/stocks/archived')).status, 200);
    response = await f.request(`/api/stocks/${good.id}/restore`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' });
    assert.equal(response.status, 200);
    assert.equal((await response.json()).stock.id, good.id);

    for (let index = 0; index < 23; index += 1) {
        const symbol = `ST${String.fromCharCode(65 + index)}`;
        response = await f.request('/api/stocks', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ symbol, name: symbol, buy_threshold_eur: 1, sell_threshold_eur: 2 }) });
        assert.equal(response.status, 201);
    }
    response = await f.request('/api/stocks', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ symbol: 'OVER', name: 'Over', buy_threshold_eur: 1, sell_threshold_eur: 2 }) });
    assert.equal(response.status, 400);
});

test('new purchases merge into an existing open position and return the authoritative aggregate', async t => {
    const f = await fixture();
    t.after(() => { f.server.close(); f.db.db.close(); fs.rmSync(f.dir, { recursive: true, force: true }); });
    const stock = (await (await bodyRequest(f, '/api/stocks', { symbol: 'MERGE', name: 'Merge', buy_threshold_eur: 10, sell_threshold_eur: 20 })).json()).stock;
    let response = await bodyRequest(f, '/api/trades', { trade_id: 'M-1', stock_id: stock.id, opened_at: '2026-09-14T10:00:00Z', quantity: 2, currency: 'EUR', entry_price: 10, entry_fx_rate: 1, entry_fee_eur: 1 });
    assert.equal(response.status, 201);
    assert.equal((await response.json()).merged, false);
    response = await bodyRequest(f, '/api/trades', { trade_id: 'M-2', stock_id: stock.id, opened_at: '2026-09-14T10:05:00Z', quantity: 3, currency: 'EUR', entry_price: 20, entry_fx_rate: 1, entry_fee_eur: 2 });
    const merged = await response.json();
    assert.equal(response.status, 201);
    assert.equal(merged.merged, true);
    assert.equal(merged.merged_count, 1);
    assert.equal(merged.trade.quantity, 5);
    assert.equal(merged.trade.entry_price_eur, 16);
    assert.equal(merged.trade.entry_fee_eur, 3);
    response = await f.request('/api/trades?status=open');
    const open = await response.json();
    assert.equal(open.filter(trade => trade.symbol === 'MERGE').length, 1);
});

async function bodyRequest(f, url, body, method = 'POST') {
    return f.request(url, { method, headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
}

test('alert-level API supports authoritative CRUD, validation, disable, rearm, and threshold rejection', async t => {
    const f = await fixture();
    t.after(() => { f.server.close(); f.db.db.close(); fs.rmSync(f.dir, { recursive: true, force: true }); });
    const created = (await (await bodyRequest(f, '/api/stocks', { symbol: 'LEVEL', name: 'Levels', buy_threshold_eur: 10, sell_threshold_eur: 20 })).json()).stock;
    assert.equal(Object.hasOwn(created, 'owned'), false);
    let response = await f.request(`/api/stocks/${created.id}/alert-levels`);
    assert.equal((await response.json()).length, 2);
    response = await bodyRequest(f, `/api/stocks/${created.id}/alert-levels`, { side: 'BUY', price_eur: 8 });
    assert.equal(response.status, 201);
    const level = (await response.json()).alert_level;
    response = await bodyRequest(f, `/api/alert-levels/${level.id}`, { side: 'SELL', price_eur: 25, enabled: false }, 'PUT');
    assert.equal(response.status, 200);
    assert.equal((await response.json()).alert_level.active, 0);
    response = await bodyRequest(f, `/api/alert-levels/${level.id}/rearm`, {});
    assert.equal((await response.json()).alert_level.enabled, 1);
    response = await bodyRequest(f, `/api/alert-levels/${level.id}`, { price_eur: 0 }, 'PUT');
    assert.equal(response.status, 400);
    response = await bodyRequest(f, `/api/alert-levels/${level.id}`, { enabled: 'not-a-boolean' }, 'PUT');
    assert.equal(response.status, 400);
    assert.match((await response.json()).error, /must be boolean/);
    response = await bodyRequest(f, `/api/stocks/${created.id}`, { buy_threshold_eur: 7 }, 'PUT');
    assert.equal(response.status, 400);
    assert.match((await response.json()).error, /manage alert levels/);
    response = await bodyRequest(f, `/api/stocks/${created.id}`, { owned: true }, 'PUT');
    assert.equal(response.status, 400);
    assert.match((await response.json()).error, /Manual ownership is no longer supported/);
    f.db.addAlertForLevel(f.db.getAlertLevelById(level.id), 25, 123);
    response = await f.request(`/api/alert-levels/${level.id}`, { method: 'DELETE' });
    assert.equal(response.status, 200);
    assert.equal(f.db.db.prepare('SELECT alert_level_id FROM alerts_log WHERE telegram_message_id=?').get('123').alert_level_id, null);
});

test('stock routes validate, update authoritatively, hard-delete, and archive journal identity', async t => {
    const f = await fixture();
    t.after(() => { f.server.close(); f.db.db.close(); fs.rmSync(f.dir, { recursive: true, force: true }); });
    assert.equal((await f.request('/api/stocks/999')).status, 404);
    let response = await bodyRequest(f, '/api/stocks', { symbol: 'HOST', name: '<img src=x onerror=alert(1)>', buy_threshold_eur: 1, sell_threshold_eur: 2 });
    const created = (await response.json()).stock;
    response = await bodyRequest(f, `/api/stocks/${created.id}`, { name: 'Updated', enabled: 0 }, 'PUT');
    assert.equal(response.status, 200);
    assert.equal((await response.json()).stock.name, 'Updated');
    assert.equal((await f.request(`/api/stocks/${created.id}`)).status, 200);
    response = await f.request(`/api/stocks/${created.id}`, { method: 'DELETE' });
    assert.equal((await response.json()).archived, false);
    assert.equal((await f.request(`/api/stocks/${created.id}`)).status, 404);

    response = await bodyRequest(f, '/api/stocks', { symbol: 'ARCH', name: 'Archive me', buy_threshold_eur: 1, sell_threshold_eur: 2 });
    const archived = (await response.json()).stock;
    await bodyRequest(f, '/api/trades', { stock_id: archived.id, opened_at: '2026-01-01T00:00:00Z', quantity: 1, currency: 'EUR', entry_price: 1, entry_fx_rate: 1, entry_fee_eur: 0 });
    response = await f.request(`/api/stocks/${archived.id}`, { method: 'DELETE' });
    assert.equal((await response.json()).archived, true);
    const list = await (await f.request('/api/stocks')).json();
    assert.equal(list.some(stock => stock.id === archived.id), false);
    assert.equal((await (await f.request('/api/stocks/archived')).json()).some(stock => stock.id === archived.id), true);
});

test('history range, restore capacity, and capital duplicate validation are explicit errors', async t => {
    const f = await fixture();
    t.after(() => { f.server.close(); f.db.db.close(); fs.rmSync(f.dir, { recursive: true, force: true }); });
    let response = await bodyRequest(f, '/api/stocks', { symbol: 'RANGE', name: 'Range', buy_threshold_eur: 1, sell_threshold_eur: 2 });
    const stock = (await response.json()).stock;
    assert.equal((await f.request(`/api/stocks/${stock.id}/history?range=bad`)).status, 400);
    await bodyRequest(f, '/api/capital-events', { event_type: 'INITIAL', amount_eur: 100, occurred_at: '2026-01-01T00:00:00Z' });
    response = await bodyRequest(f, '/api/capital-events', { event_type: 'INITIAL', amount_eur: 200, occurred_at: '2026-01-02T00:00:00Z' });
    assert.equal(response.status, 409);
    const duplicateCapital = await response.json();
    assert.equal(duplicateCapital.code, 'INITIAL_CAPITAL_EXISTS');
    assert.match(duplicateCapital.error, /Use a deposit/i);
    await bodyRequest(f, '/api/trades', { stock_id: stock.id, opened_at: '2026-01-01T00:00:00Z', quantity: 1, currency: 'EUR', entry_price: 1, entry_fx_rate: 1 });
    const archived = (await (await f.request(`/api/stocks/${stock.id}`, { method: 'DELETE' })).json()).archived;
    assert.equal(archived, true);
    for (let i = 0; i < 25; i += 1) {
        const symbol = `C${String.fromCharCode(65 + Math.floor(i / 26))}${String.fromCharCode(65 + i)}`;
        await bodyRequest(f, '/api/stocks', { symbol, name: `Cap ${i}`, buy_threshold_eur: 1, sell_threshold_eur: 2 });
    }
    const full = (await (await f.request('/api/stocks/archived')).json());
    assert.equal(full.some(item => item.id === stock.id), true);
    response = await bodyRequest(f, `/api/stocks/${stock.id}/restore`, {});
    assert.equal(response.status, 400);
});

test('paginated trade endpoint returns twenty entries with stable totals', async t => {
    const f = await fixture();
    t.after(() => { f.server.close(); f.db.db.close(); fs.rmSync(f.dir, { recursive: true, force: true }); });
    const stock = (await (await bodyRequest(f, '/api/stocks', { symbol: 'PAGE', name: 'Pagination', buy_threshold_eur: 1, sell_threshold_eur: 2 })).json()).stock;
    const insert = f.db.db.prepare(`INSERT INTO trades (
        trade_id, stock_id, opened_at, closed_at, quantity, currency,
        entry_price, entry_fx_rate, entry_price_eur, entry_fee_eur,
        exit_price, exit_fx_rate, exit_price_eur, exit_fee_eur, dividend_eur, strategy, notes
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`);
    for (let day = 1; day <= 25; day += 1) {
        insert.run(`PAGE-${day}`, stock.id, `2026-01-${String(day).padStart(2, '0')} 10:00:00`, '2026-02-01 10:00:00', 1, 'EUR', 10, 1, 10, 0, 11, 1, 11, 0, 0, '', '');
    }
    let response = await f.request('/api/trades?page=1&page_size=20');
    assert.equal(response.status, 200);
    let page = await response.json();
    assert.equal(page.total, 25);
    assert.equal(page.total_pages, 2);
    assert.equal(page.page, 1);
    assert.equal(page.page_size, 20);
    assert.equal(page.items.length, 20);
    assert.equal(page.items[0].trade_id, 'PAGE-25');

    response = await f.request('/api/trades?page=2&page_size=20');
    page = await response.json();
    assert.equal(page.items.length, 5);
    assert.equal(page.items[0].trade_id, 'PAGE-5');
    assert.equal((await f.request('/api/trades?page=0')).status, 400);
});

test('trade GET and PUT return authoritative records and reject invalid stock references', async t => {
    const f = await fixture();
    t.after(() => { f.server.close(); f.db.db.close(); fs.rmSync(f.dir, { recursive: true, force: true }); });
    const stock = (await (await bodyRequest(f, '/api/stocks', { symbol: 'TRADE', name: 'Trade', buy_threshold_eur: 1, sell_threshold_eur: 2 })).json()).stock;
    let response = await bodyRequest(f, '/api/trades', { stock_id: 9999, opened_at: '2026-01-01T00:00:00Z', quantity: 1, currency: 'EUR', entry_price: 1, entry_fx_rate: 1 });
    assert.equal(response.status, 400);
    response = await bodyRequest(f, '/api/trades', { trade_id: 'PUT-1', stock_id: stock.id, opened_at: '2026-01-01T00:00:00Z', quantity: 1, currency: 'EUR', entry_price: 1, entry_fx_rate: 1 });
    const trade = (await response.json()).trade;
    assert.equal((await f.request('/api/trades')).status, 200);
    response = await bodyRequest(f, `/api/trades/${trade.id}`, { notes: 'changed', stock_id: stock.id, opened_at: '2026-01-01T00:00:00Z', quantity: 1, currency: 'EUR', entry_price: 1, entry_fx_rate: 1 }, 'PUT');
    assert.equal(response.status, 200);
    assert.equal((await response.json()).trade.notes, 'changed');
});

test('journal records a partial sale while preserving the remaining open position and proportional entry fees', async t => {
    const f = await fixture();
    t.after(() => { f.server.close(); f.db.db.close(); fs.rmSync(f.dir, { recursive: true, force: true }); });
    const created = await bodyRequest(f, '/api/stocks', { symbol: 'PART', name: 'Partial', buy_threshold_eur: 10, sell_threshold_eur: 20 });
    const stock = (await created.json()).stock;
    const opened = await bodyRequest(f, '/api/trades', {
        trade_id: 'PART-OPEN', stock_id: stock.id, opened_at: '2026-09-20T10:00:00Z', quantity: 10,
        currency: 'EUR', entry_price: 50, entry_fx_rate: 1, entry_fee_eur: 5
    });
    const openTrade = (await opened.json()).trade;
    const response = await bodyRequest(f, `/api/trades/${openTrade.id}/close`, {
        close_quantity: 4, closed_at: '2026-09-21T10:00:00Z', exit_price: 60, exit_fx_rate: 1,
        exit_fee_eur: 2, dividend_eur: 1, notes: 'Trim position'
    });
    assert.equal(response.status, 200);
    const result = await response.json();
    assert.equal(result.partial, true);
    assert.equal(result.trade.quantity, 4);
    assert.equal(result.trade.entry_fee_eur, 2);
    assert.equal(result.trade.exit_fee_eur, 2);
    assert.equal(result.open_trade.quantity, 6);
    assert.equal(result.open_trade.entry_fee_eur, 3);
    assert.equal(result.open_trade.is_closed, false);
    assert.match(result.trade.notes, /Partial sale of 4 from open trade PART-OPEN/);
    assert.equal(f.db.getPositionSummary(stock.id).quantity, 6);
    assert.equal(f.db.getAllTrades({ status: 'closed' }).length, 1);

    const oversell = await bodyRequest(f, `/api/trades/${openTrade.id}/close`, {
        close_quantity: 7, closed_at: '2026-09-22T10:00:00Z', exit_price: 61, exit_fx_rate: 1, exit_fee_eur: 0
    });
    assert.equal(oversell.status, 400);
    assert.equal(f.db.getPositionSummary(stock.id).quantity, 6);
});
