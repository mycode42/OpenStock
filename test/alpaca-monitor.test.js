const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const Database = require('better-sqlite3');
const { initDatabase } = require('../database/init');
const { runAlpacaMonitor } = require('../lib/alpaca-monitor');
const { AlpacaError } = require('../lib/brokers/alpaca');

const ENABLED = { };

function makeDb(t) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-alpaca-monitor-'));
    const file = path.join(dir, 'test.db');
    initDatabase({ dbPath: file, seedDefaults: false, silent: true });
    t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
    const db = new Database(file);
    db.pragma('foreign_keys = OFF');
    t.after(() => db.close());
    return { db, lockPath: path.join(dir, 'monitor.lock') };
}

function seedLevel(db, { side = 'BUY', price = 100, autoOrder = 0 } = {}) {
    const watchlistId = Number(db.prepare(`INSERT INTO alpaca_watchlist (symbol, name, source_stock_id, copied_fx_rate)
        VALUES ('AAPL', 'Apple', 1, 1)`).run().lastInsertRowid);
    return Number(db.prepare(`INSERT INTO alpaca_alert_levels (watchlist_id, source_alert_level_id, side, price_usd, auto_order, order_budget_usd)
        VALUES (?, 1, ?, ?, ?, ?)`).run(watchlistId, side, price, autoOrder, autoOrder ? 250 : null).lastInsertRowid);
}

function fakeClient({ bid = 98, ask = 99 } = {}) {
    const submitted = [];
    return {
        submitted,
        isConfigured: () => true,
        async getLatestQuotes(symbols) {
            return { feed: 'iex', quotes: Object.fromEntries(symbols.map(s => [s, { bp: bid, ap: ask, t: '2026-10-29T10:00:00Z' }])) };
        },
        async getPositions() { return []; },
        async submitOrder(body) { submitted.push(body); return { id: `broker-${submitted.length}`, status: 'new' }; },
        async getOrder(id) { return { id, status: 'new' }; },
        async getOrderByClientId() { throw new AlpacaError('not found', 404); },
    };
}

function telegram() {
    const sent = [];
    const fetchImpl = async (url, { body }) => {
        sent.push(new URLSearchParams(body).get('text'));
        return { ok: true, status: 200, json: async () => ({ ok: true, result: { message_id: sent.length } }) };
    };
    return { sent, fetchImpl };
}

function run({ db, client, lockPath, env, fetchImpl }) {
    return runAlpacaMonitor({
        db, alpacaClient: client, env, config: { lockPath, telegram: { token: 't', chatId: 'c' } }, fetchImpl,
        now: () => new Date('2026-10-29T10:00:00Z'),
    });
}

test('a level with real orders on places one limit order when it fires and alerts once', async t => {
    const { db, lockPath } = makeDb(t);
    const levelId = seedLevel(db, { autoOrder: 1 });
    const client = fakeClient();
    const tg = telegram();

    await run({ db, client, lockPath, env: ENABLED, fetchImpl: tg.fetchImpl });
    assert.equal(client.submitted.length, 1);
    assert.equal(client.submitted[0].side, 'buy');
    assert.equal(client.submitted[0].type, 'limit');
    assert.equal(client.submitted[0].qty, '2');
    assert.equal(client.submitted[0].limit_price, '100.00');
    assert.equal(db.prepare('SELECT COUNT(*) AS count FROM alpaca_orders WHERE alpaca_alert_level_id=?').get(levelId).count, 1);
    assert.equal(tg.sent.length, 1);
    assert.match(tg.sent[0], /Real order \(paper\): BUY \d+ @ \$/);

    // A second poll sees the retired level: no new order and no repeated Telegram alert.
    await run({ db, client, lockPath, env: ENABLED, fetchImpl: tg.fetchImpl });
    assert.equal(client.submitted.length, 1);
    assert.equal(tg.sent.length, 1);
});

test('a level without real orders alerts but never places an order', async t => {
    const { db, lockPath } = makeDb(t);
    seedLevel(db, { autoOrder: 0 });
    const client = fakeClient();
    const tg = telegram();

    await run({ db, client, lockPath, env: ENABLED, fetchImpl: tg.fetchImpl });
    assert.equal(client.submitted.length, 0);
    assert.equal(tg.sent.length, 1);
    assert.doesNotMatch(tg.sent[0], /Real order/);
});

test('a failed alert delivery stays in the outbox and is retried before the level retires', async t => {
    const { db, lockPath } = makeDb(t);
    const levelId = seedLevel(db, { price: 100 });
    const client = fakeClient();
    let fail = true;
    const sent = [];
    const fetchImpl = async (url, { body }) => {
        if (fail) return { ok: true, status: 200, json: async () => ({ ok: false }) };
        sent.push(new URLSearchParams(body).get('text'));
        return { ok: true, status: 200, json: async () => ({ ok: true, result: { message_id: 77 } }) };
    };

    assert.equal(await run({ db, client, lockPath, env: ENABLED, fetchImpl }), 1);
    assert.equal(db.prepare('SELECT status FROM alpaca_alert_outbox').get().status, 'FAILED');
    // The level retires when its alert is queued; the outbox row carries the retry.
    assert.equal(db.prepare('SELECT triggered, retired FROM alpaca_alert_levels WHERE id=?').get(levelId).retired, 1);

    fail = false;
    assert.equal(await run({ db, client, lockPath, env: ENABLED, fetchImpl }), 0);
    assert.deepEqual(db.prepare('SELECT status, attempt_count, telegram_message_id FROM alpaca_alert_outbox').get(),
        { status: 'SENT', attempt_count: 2, telegram_message_id: '77' });
    assert.equal(sent.length, 1);
});

test('an ambiguous Telegram send stays frozen and is not automatically duplicated', async t => {
    const { db, lockPath } = makeDb(t);
    const levelId = seedLevel(db, { price: 100 });
    const client = fakeClient();
    let deliveries = 0;
    const fetchImpl = async () => {
        deliveries += 1;
        throw new Error('connection reset after request');
    };

    assert.equal(await run({ db, client, lockPath, env: ENABLED, fetchImpl }), 1);
    assert.equal(db.prepare('SELECT status FROM alpaca_alert_outbox').get().status, 'SENDING');
    // The frozen SENDING row is left for a human to resolve; the next poll neither resends nor fails.
    assert.equal(await run({ db, client, lockPath, env: ENABLED, fetchImpl }), 0);
    assert.equal(deliveries, 1);
    assert.equal(db.prepare('SELECT retired FROM alpaca_alert_levels WHERE id=?').get(levelId).retired, 1);
});

test('a rearmed level notifies again on its next firing, with its own outbox row', async t => {
    const { db, lockPath } = makeDb(t);
    const levelId = seedLevel(db, { autoOrder: 0 });
    const client = fakeClient();
    const tg = telegram();
    const runAt = minute => runAlpacaMonitor({
        db, alpacaClient: client, env: ENABLED, config: { lockPath, telegram: { token: 't', chatId: 'c' } }, fetchImpl: tg.fetchImpl,
        now: () => new Date(`2026-10-29T10:${String(minute).padStart(2, '0')}:00Z`),
    });

    await runAt(0);
    assert.equal(tg.sent.length, 1);

    db.prepare(`UPDATE alpaca_alert_levels SET enabled=1, active=1, triggered=0, retired=0, triggered_at=NULL, retired_at=NULL WHERE id=?`).run(levelId);
    await runAt(5);
    assert.equal(tg.sent.length, 2, 'the rearmed level must notify');
    assert.equal(db.prepare('SELECT COUNT(*) AS count FROM alpaca_alert_outbox WHERE alpaca_alert_level_id=?').get(levelId).count, 2);
});

test('without a Telegram recipient the alert stays pending, with no send attempt and no per-cycle error', async t => {
    const { db, lockPath } = makeDb(t);
    seedLevel(db, { price: 100 });
    const client = fakeClient();
    let calls = 0;
    const fetchImpl = async () => { calls += 1; return { ok: true, status: 200, json: async () => ({ ok: true }) }; };
    const runWithoutTelegram = () => runAlpacaMonitor({
        db, alpacaClient: client, env: ENABLED, config: { lockPath, telegram: { token: '', chatId: '' } }, fetchImpl,
    });
    assert.equal(await runWithoutTelegram(), 0);
    assert.equal(await runWithoutTelegram(), 0);
    assert.equal(calls, 0);
    assert.equal(db.prepare('SELECT status, attempt_count FROM alpaca_alert_outbox').get().status, 'PENDING');
});

test('a delivery that keeps failing is retried up to the limit, then left in the outbox', async t => {
    const { db, lockPath } = makeDb(t);
    seedLevel(db, { price: 100 });
    const client = fakeClient();
    const fetchImpl = async () => ({ ok: true, status: 200, json: async () => ({ ok: false }) });
    for (let cycle = 0; cycle < 12; cycle += 1) await run({ db, client, lockPath, env: ENABLED, fetchImpl });
    assert.deepEqual(db.prepare('SELECT status, attempt_count FROM alpaca_alert_outbox').get(), { status: 'FAILED', attempt_count: 10 });
});

const { lateLabel, parseStoredUtc } = require('../lib/alpaca-monitor');

// SQLite's default timestamp: UTC, no zone marker.
function sqliteUtc(date) {
    return date.toISOString().replace('T', ' ').slice(0, 19);
}

test('stored queue times are read as UTC whatever the host zone is', () => {
    assert.equal(parseStoredUtc('2026-11-06 17:26:56'), Date.parse('2026-11-06T17:26:56Z'));
    assert.equal(parseStoredUtc('2026-11-06T17:26:56.000Z'), Date.parse('2026-11-06T17:26:56Z'));
});

test('a fresh alert gets no late label, and an alert queued 20 minutes ago gets one with its UTC time', () => {
    const now = new Date('2026-11-06T17:30:00Z');
    assert.equal(lateLabel({ created_at: sqliteUtc(new Date(now.getTime() - 5_000)) }, now), '');
    const label = lateLabel({ created_at: '2026-11-06 17:10:00' }, now);
    assert.match(label, /<b>Late alert<\/b>, queued 2026-11-06 17:10:00 UTC/);
});

test('an alert queued without Telegram and sent 20 minutes later carries the late label', async t => {
    const { db, lockPath } = makeDb(t);
    seedLevel(db, { price: 100 });
    const client = fakeClient();
    const queuedAt = new Date('2026-11-06T17:00:00Z');
    const sent = [];
    const fetchImpl = async (url, { body }) => {
        sent.push(new URLSearchParams(body).get('text'));
        return { ok: true, status: 200, json: async () => ({ ok: true, result: { message_id: 5 } }) };
    };
    await runAlpacaMonitor({ db, alpacaClient: client, env: ENABLED, config: { lockPath, telegram: { token: '', chatId: '' } }, fetchImpl, now: () => queuedAt });
    // The outbox stamps created_at with SQLite's clock; set it to the queue time the test simulates.
    db.prepare("UPDATE alpaca_alert_outbox SET created_at='2026-11-06 17:00:00'").run();
    await runAlpacaMonitor({ db, alpacaClient: client, env: ENABLED, config: { lockPath, telegram: { token: 't', chatId: 'c' } }, fetchImpl, now: () => new Date(queuedAt.getTime() + 20 * 60_000) });
    assert.equal(sent.length, 1);
    assert.match(sent[0], /^<b>Late alert<\/b>, queued 2026-11-06 17:00:00 UTC/);
});

test('an unknown send result is reported once per process and never resent', async t => {
    const { db, lockPath } = makeDb(t);
    seedLevel(db, { price: 100 });
    const client = fakeClient();
    let calls = 0;
    const fetchImpl = async () => { calls += 1; throw new Error('socket hang up'); };
    const errors = [];
    const once = () => runAlpacaMonitor({
        db, alpacaClient: client, env: ENABLED, config: { lockPath, telegram: { token: 't', chatId: 'c' } }, fetchImpl,
        error: message => errors.push(message),
    });
    await once();
    await once();
    await once();
    assert.equal(calls, 1);
    assert.equal(errors.filter(message => message.includes('unknown delivery result')).length, 1);
});
