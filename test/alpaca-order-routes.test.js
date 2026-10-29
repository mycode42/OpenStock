const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const Database = require('better-sqlite3');
const { initDatabase } = require('../database/init');
const OpenStockDB = require('../database/db');
const { createApp } = require('../server');

const TOKEN = 'route-test-token-value';

async function fixture(t) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-order-routes-'));
    const file = path.join(dir, 'test.db');
    initDatabase({ dbPath: file, seedDefaults: false, silent: true });
    const raw = new Database(file);
    raw.pragma('foreign_keys = OFF');
    raw.prepare(`INSERT INTO alpaca_watchlist (id, symbol, name, source_stock_id, copied_fx_rate) VALUES (1, 'AAPL', 'Apple', 1, 1)`).run();
    const levelId = Number(raw.prepare(`INSERT INTO alpaca_alert_levels (watchlist_id, source_alert_level_id, side, price_usd)
        VALUES (1, 1, 'BUY', 100)`).run().lastInsertRowid);
    raw.close();
    const db = new OpenStockDB(file);
    const backfill = { enqueue() {}, enqueueMissing() { return 0; } };
    const app = createApp({ db, backfill, fetchImpl: async () => { throw new Error('unexpected fetch'); }, sdkClient: { isEnabled: () => false } });
    app.locals.alpacaClient = {
        submitted: [],
        async getLatestQuotes() { return { feed: 'iex', quotes: { AAPL: { bp: 99, ap: 101 } } }; },
        async getPositions() { return []; },
        async submitOrder(body) { this.submitted.push(body); return { id: 'broker-1', status: 'accepted' }; },
    };
    const server = await new Promise(resolve => { const listener = app.listen(0, '127.0.0.1', () => resolve(listener)); });
    t.after(() => { server.close(); db.db.close(); fs.rmSync(dir, { recursive: true, force: true }); });
    const base = `http://127.0.0.1:${server.address().port}`;
    return { base, levelId, client: app.locals.alpacaClient };
}

function withEnv(t, env) {
    const saved = { ALPACA_MANUAL_BASE_URL: process.env.ALPACA_MANUAL_BASE_URL };
    for (const [key, value] of Object.entries(env)) { if (value === undefined) delete process.env[key]; else process.env[key] = value; }
    t.after(() => { for (const [key, value] of Object.entries(saved)) { if (value === undefined) delete process.env[key]; else process.env[key] = value; } });
}

const post = (base, url, headers, body) => fetch(base + url, {
    method: 'POST', headers: { 'Content-Type': 'application/json', ...headers }, body: JSON.stringify(body),
});

test('preview returns the plan, and submit needs explicit confirmation', async t => {
    withEnv(t, { ALPACA_MANUAL_BASE_URL: undefined });
    const { base, levelId, client } = await fixture(t);
    const headers = {};
    const preview = await post(base, `/api/alpaca/alert-levels/${levelId}/orders`, headers, { preview: true });
    assert.equal(preview.status, 200);
    assert.equal((await preview.json()).plan.mode, 'paper');
    assert.equal(client.submitted.length, 0);

    assert.equal((await post(base, `/api/alpaca/alert-levels/${levelId}/orders`, headers, {})).status, 400);
    const placed = await post(base, `/api/alpaca/alert-levels/${levelId}/orders`, headers, { confirm: true });
    assert.equal(placed.status, 201);
    assert.equal((await placed.json()).order.status, 'NEW');
    assert.equal(client.submitted.length, 1);
});

test('config endpoint reports paper mode and never returns the token', async t => {
    withEnv(t, { ALPACA_MANUAL_BASE_URL: undefined });
    const { base } = await fixture(t);
    const text = await (await fetch(`${base}/api/alpaca/orders/config`)).text();
    const body = JSON.parse(text);
    assert.equal(body.orders.mode, 'paper');
});

test('real-order switch needs the order token, a boolean, and a BUY budget', async t => {
    withEnv(t, { ALPACA_MANUAL_BASE_URL: undefined });
    const { base, levelId } = await fixture(t);
    const put = (headers, body) => fetch(`${base}/api/alpaca/alert-levels/${levelId}/order`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json', ...headers }, body: JSON.stringify(body),
    });
    const auth = {};
    assert.equal((await put(auth, { auto_order: 'yes' })).status, 400);
    assert.equal((await put(auth, { auto_order: true })).status, 400);
    const response = await put(auth, { auto_order: true, budget_usd: 250 });
    assert.equal(response.status, 200);
    const level = (await response.json()).alert_level;
    assert.equal(level.auto_order, 1);
    assert.equal(level.order_budget_usd, 250);
});

test('live orders need ALPACA_ALLOW_LIVE and the order token; paper needs neither', async t => {
    const keys = ['ALPACA_MANUAL_BASE_URL', 'ALPACA_ALLOW_LIVE', 'OPENSTOCK_ORDER_TOKEN'];
    const saved = Object.fromEntries(keys.map(key => [key, process.env[key]]));
    t.after(() => { for (const key of keys) { if (saved[key] === undefined) delete process.env[key]; else process.env[key] = saved[key]; } });
    process.env.OPENSTOCK_ORDER_TOKEN = TOKEN;
    const { base, levelId } = await fixture(t);
    const auth = { 'X-OpenStock-Order-Token': TOKEN };
    const preview = headers => post(base, `/api/alpaca/alert-levels/${levelId}/orders`, headers, { preview: true });

    process.env.ALPACA_MANUAL_BASE_URL = 'https://api.alpaca.markets/v2';
    assert.equal((await preview(auth)).status, 403, 'live without the opt-in is refused');
    process.env.ALPACA_ALLOW_LIVE = '1';
    assert.equal((await preview({})).status, 401, 'live without the token is refused');
    assert.equal((await preview({ 'X-OpenStock-Order-Token': 'nope' })).status, 401);
    assert.equal((await preview(auth)).status, 200, 'live with the opt-in and the token previews');
    const enable = await fetch(`${base}/api/alpaca/alert-levels/${levelId}/order`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ auto_order: true, budget_usd: 250 }),
    });
    assert.equal(enable.status, 401, 'switching real orders on for live needs the token');

    delete process.env.ALPACA_MANUAL_BASE_URL;
    delete process.env.ALPACA_ALLOW_LIVE;
    assert.equal((await preview({})).status, 200, 'paper needs no token');
});
