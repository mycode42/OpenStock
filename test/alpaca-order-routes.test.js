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
    const saved = { ORDERS_ENABLED: process.env.ORDERS_ENABLED, OPENSTOCK_ORDER_TOKEN: process.env.OPENSTOCK_ORDER_TOKEN, ALPACA_LIVE_BASE_URL: process.env.ALPACA_LIVE_BASE_URL };
    for (const [key, value] of Object.entries(env)) { if (value === undefined) delete process.env[key]; else process.env[key] = value; }
    t.after(() => { for (const [key, value] of Object.entries(saved)) { if (value === undefined) delete process.env[key]; else process.env[key] = value; } });
}

const post = (base, url, headers, body) => fetch(base + url, {
    method: 'POST', headers: { 'Content-Type': 'application/json', ...headers }, body: JSON.stringify(body),
});

test('order routes refuse everything while ORDERS_ENABLED is off', async t => {
    withEnv(t, { ORDERS_ENABLED: 'false', OPENSTOCK_ORDER_TOKEN: TOKEN, ALPACA_LIVE_BASE_URL: undefined });
    const { base, levelId, client } = await fixture(t);
    const response = await post(base, `/api/alpaca/alert-levels/${levelId}/orders`, { 'X-OpenStock-Order-Token': TOKEN }, { confirm: true });
    assert.equal(response.status, 403);
    assert.equal(client.submitted.length, 0);
});

test('wrong or missing token is rejected', async t => {
    withEnv(t, { ORDERS_ENABLED: 'true', OPENSTOCK_ORDER_TOKEN: TOKEN });
    const { base, levelId, client } = await fixture(t);
    assert.equal((await post(base, `/api/alpaca/alert-levels/${levelId}/orders`, {}, { confirm: true })).status, 401);
    assert.equal((await post(base, `/api/alpaca/alert-levels/${levelId}/orders`, { 'X-OpenStock-Order-Token': 'nope' }, { confirm: true })).status, 401);
    assert.equal(client.submitted.length, 0);
});

test('preview returns the plan, and submit needs explicit confirmation', async t => {
    withEnv(t, { ORDERS_ENABLED: 'true', OPENSTOCK_ORDER_TOKEN: TOKEN, ALPACA_LIVE_BASE_URL: undefined });
    const { base, levelId, client } = await fixture(t);
    const headers = { 'X-OpenStock-Order-Token': TOKEN };
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
    withEnv(t, { ORDERS_ENABLED: 'true', OPENSTOCK_ORDER_TOKEN: TOKEN, ALPACA_LIVE_BASE_URL: undefined });
    const { base } = await fixture(t);
    const text = await (await fetch(`${base}/api/alpaca/orders/config`)).text();
    const body = JSON.parse(text);
    assert.equal(body.orders.mode, 'paper');
    assert.equal(body.orders.token_configured, true);
    assert.ok(!text.includes(TOKEN));
});
