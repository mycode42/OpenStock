const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { initDatabase } = require('../database/init');
const OpenStockDB = require('../database/db');
const { createApp, berlinTradingDayStart } = require('../server');

async function fixture({ now } = {}) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-api-'));
    const file = path.join(dir, 'test.db');
    initDatabase({ dbPath: file, seedDefaults: false, silent: true });
    const db = new OpenStockDB(file);
    const backfill = { enqueue() {}, enqueueMissing() { return 0; } };
    const fetchImpl = async url => {
        if (url.includes('exchangerate')) return { ok: true, json: async () => ({ rates: { EUR: 0.9 } }) };
        const symbol = decodeURIComponent(url.split('/chart/')[1].split('?')[0]);
        if (symbol === 'BAD') return { ok: false, status: 503, json: async () => ({}) };
        return { ok: true, json: async () => ({ chart: { result: [{ meta: { shortName: symbol }, timestamp: [1767225600], indicators: { quote: [{ close: [100] }] } }] } }) };
    };
    const app = createApp({ db, backfill, fetchImpl, now });
    app.locals.brokerClient = {
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
    return { db, dir, server, request };
}

test('Berlin trading day starts at 07:30 with daylight-saving offsets', () => {
    assert.equal(berlinTradingDayStart(new Date('2026-09-16T15:45:00Z')).toISOString(), '2026-09-16T05:30:00.000Z');
    assert.equal(berlinTradingDayStart(new Date('2026-12-16T12:00:00Z')).toISOString(), '2026-12-16T06:30:00.000Z');
    assert.equal(berlinTradingDayStart(new Date('2026-09-16T04:00:00Z')).toISOString(), '2026-09-15T05:30:00.000Z');
});

test('one-day history starts at 07:30 Berlin time and uses fifteen-minute buckets', async t => {
    const f = await fixture({ now: () => new Date('2026-09-16T15:45:00Z') });
    t.after(() => { f.server.close(); f.db.db.close(); fs.rmSync(f.dir, { recursive: true, force: true }); });
    const response = await f.request('/api/stocks', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ symbol: 'DAY', name: 'Trading day', buy_threshold_eur: 80, sell_threshold_eur: 120 }) });
    const stock = (await response.json()).stock;
    f.db.addPriceSnapshot(stock.id, null, 99, null, 'trade_republic_history', '2026-09-16T05:20:00.000Z', '2026-09-16 05:20:00');
    for (let minute = 0; minute < 60; minute += 1) {
        const timestamp = new Date(Date.parse('2026-09-16T05:30:00.000Z') + minute * 60000).toISOString();
        f.db.addPriceSnapshot(stock.id, null, 100 + minute, null, 'trade_republic_history', timestamp, timestamp.replace('T', ' ').replace('.000Z', ''));
    }
    const history = await (await f.request(`/api/stocks/${stock.id}/history?range=1d`)).json();
    assert.equal(history.start_at, '2026-09-16T05:30:00.000Z');
    assert.equal(history.bucket_seconds, 900);
    assert.equal(history.points.length, 48);
    assert.equal(history.points[0].recorded_at, '2026-09-16T05:30:00.000Z');
    assert.equal(history.points.at(-1).recorded_at, '2026-09-16T06:29:00.000Z');
    assert.equal(history.points[0].price_eur, 100);
    assert.equal(history.points.at(-1).price_eur, 159);
});

test('API factory covers lifecycle, history, journal, analytics, refresh isolation, and archive restore', async t => {
    const f = await fixture();
    t.after(() => { f.server.close(); f.db.db.close(); fs.rmSync(f.dir, { recursive: true, force: true }); });
    let response = await f.request('/health');
    assert.equal(response.status, 200);
    assert.match(response.headers.get('cache-control'), /no-store/);
    assert.equal(response.headers.get('x-openstock-version'), '1.0.0');
    assert.equal((await response.json()).version, '1.0.0');

    response = await f.request('/api/stocks', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ symbol: 'GOOD', name: 'Good <Stock>', buy_threshold_eur: 80, sell_threshold_eur: 120 }) });
    assert.equal(response.status, 201);
    const good = (await response.json()).stock;
    f.db.addPriceSnapshot(good.id, 100, 90, 0.9, 'yahoo_regular_close', null, '2026-01-01 12:00:00');
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
    response = await f.request(`/api/trades/${trade.id}/close`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ closed_at: '2026-01-03T00:00:00Z', exit_price: 100, exit_fx_rate: 1, exit_fee_eur: 1, dividend_eur: 2 }) });
    assert.equal(response.status, 200);
    assert.equal((await f.request('/api/analytics/dashboard')).status, 200);

    const bad = await f.request('/api/stocks', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ symbol: 'BAD', name: 'Bad', buy_threshold_eur: 1, sell_threshold_eur: 2 }) });
    assert.equal(bad.status, 201);
    response = await f.request('/api/refresh-prices', { method: 'POST' });
    const refreshed = await response.json();
    assert.equal(response.status, 200);
    assert.equal(refreshed.updated, 1);
    const fallbackStock = refreshed.stocks.find(stock => stock.id === good.id);
    assert.equal(fallbackStock.current_price_usd, 100);
    assert.equal(fallbackStock.current_price_eur, 90);
    assert.equal(fallbackStock.last_exchange_rate, 0.9);
    assert.equal(fallbackStock.broker_quote, null);
    assert.deepEqual({ label: fallbackStock.market_data.label, fallback: fallbackStock.market_data.fallback }, { label: 'Yahoo Finance fallback', fallback: true });

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
    assert.equal(response.status, 400);
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
