const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const Database = require('better-sqlite3');
const { initDatabase } = require('../database/init');
const { DEPOSIT_USD, seedDemoWatchlist } = require('../server/demo-seed');
const { createDemoBroker } = require('../server/demo-broker');
const demoData = require('../server/demo-data');
const { AlpacaError } = require('../lib/brokers/alpaca');
const { buildJournalAnalytics } = require('../lib/alpaca-journal');
const { cancelOpenOrder, placeLevelOrder, syncOpenOrders } = require('../lib/alpaca-orders');

const NOW = new Date('2026-10-30T10:00:00Z');

function makeDb(t) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-demo-mode-'));
    const file = path.join(dir, 'demo.db');
    initDatabase({ dbPath: file, seedDefaults: false, silent: true });
    t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
    const db = new Database(file);
    t.after(() => db.close());
    return db;
}

test('the demo seed creates the watchlist, its levels and a retired level once', t => {
    const db = makeDb(t);
    assert.equal(seedDemoWatchlist(db, NOW), true);
    assert.equal(seedDemoWatchlist(db, NOW), false);
    assert.equal(db.prepare('SELECT COUNT(*) AS count FROM alpaca_watchlist').get().count, 6);
    assert.equal(db.prepare('SELECT COUNT(*) AS count FROM alpaca_alert_levels WHERE retired = 1').get().count, 1);
    const autoOrder = db.prepare('SELECT order_budget_usd AS budget_usd FROM alpaca_alert_levels WHERE auto_order = 1').get();
    assert.equal(autoOrder.budget_usd, 500);
});

test('the simulated broker never accepts orders outside the demo database', async () => {
    const broker = createDemoBroker({ now: () => NOW, ordersEnabled: false });
    await assert.rejects(
        broker.submitOrder({ symbol: 'AAPL', qty: '1', side: 'buy', type: 'limit', limit_price: '200', client_order_id: 'x' }),
        error => error instanceof AlpacaError && error.status === 409,
    );
});

test('a marketable demo order fills on the next status check and updates the position', async t => {
    const db = makeDb(t);
    seedDemoWatchlist(db, NOW);
    const broker = createDemoBroker({ now: () => NOW, ordersEnabled: true });
    const tesla = db.prepare(`SELECT id FROM alpaca_alert_levels WHERE price_usd = 396`).get();
    const { order } = await placeLevelOrder({ db, client: broker, levelId: tesla.id, amountUsd: 1000, now: () => NOW });
    assert.equal(order.status, 'NEW');
    await syncOpenOrders({ db, client: broker, now: () => NOW });
    const filled = db.prepare('SELECT status, filled_qty, filled_avg_price_usd FROM alpaca_orders WHERE id = ?').get(order.id);
    assert.deepEqual({ ...filled }, { status: 'FILLED', filled_qty: 2, filled_avg_price_usd: 396 });
    const positions = await broker.getPositions();
    assert.equal(positions.find(row => row.symbol === 'TSLA').qty, '2');
});

test('a resting demo order can be cancelled and then reads as cancelled', async t => {
    const db = makeDb(t);
    seedDemoWatchlist(db, NOW);
    const broker = createDemoBroker({ now: () => NOW, ordersEnabled: true });
    const level = db.prepare('SELECT id FROM alpaca_alert_levels WHERE price_usd = 124').get();
    const { order } = await placeLevelOrder({ db, client: broker, levelId: level.id, amountUsd: 500, now: () => NOW });
    await cancelOpenOrder({ db, client: broker, orderId: order.id });
    await syncOpenOrders({ db, client: broker, now: () => NOW });
    assert.equal(db.prepare('SELECT status FROM alpaca_orders WHERE id = ?').get(order.id).status, 'CANCELED');
});

test('demo quotes are simulated and labelled as demo data', async () => {
    const broker = createDemoBroker({ now: () => NOW, ordersEnabled: false });
    const payload = await broker.getLatestQuotes(['aapl', 'UNKNOWN']);
    assert.equal(payload.feed, 'simulated');
    assert.ok(payload.quotes.AAPL.bp < payload.quotes.AAPL.ap);
    assert.equal(payload.quotes.UNKNOWN, undefined);
});

// Orders are stored with the real clock, so a restart test runs on a clock ahead of it.
const LATER = new Date(Date.now() + 60 * 60_000);
const STORED_ORDER_COLUMNS = `SELECT broker_order_id, client_order_id, symbol, side, qty, limit_price_usd, status,
    filled_avg_price_usd, submitted_at, updated_at FROM alpaca_orders WHERE broker_order_id IS NOT NULL`;

test('a restarted demo keeps its filled positions and can still cancel an open order', async t => {
    const db = makeDb(t);
    seedDemoWatchlist(db, LATER);
    const broker = createDemoBroker({ now: () => LATER, ordersEnabled: true });
    const tesla = db.prepare('SELECT id FROM alpaca_alert_levels WHERE price_usd = 396').get();
    await placeLevelOrder({ db, client: broker, levelId: tesla.id, amountUsd: 1000, now: () => LATER });
    await syncOpenOrders({ db, client: broker, now: () => LATER });
    const resting = db.prepare('SELECT id FROM alpaca_alert_levels WHERE price_usd = 124').get();
    const { order } = await placeLevelOrder({ db, client: broker, levelId: resting.id, amountUsd: 500, now: () => LATER });

    const restarted = createDemoBroker({ now: () => LATER, ordersEnabled: true, storedOrders: db.prepare(STORED_ORDER_COLUMNS).all() });
    const positions = await restarted.getPositions();
    assert.equal(positions.find(row => row.symbol === 'TSLA').qty, '2');
    assert.equal(positions.find(row => row.symbol === 'AAPL').qty, '8');
    await cancelOpenOrder({ db, client: restarted, orderId: order.id });
    await syncOpenOrders({ db, client: restarted, now: () => LATER });
    assert.equal(db.prepare('SELECT status FROM alpaca_orders WHERE id = ?').get(order.id).status, 'CANCELED');
});

test('the demo account, equity curve and journal agree on return and closed trades', async t => {
    const db = makeDb(t);
    seedDemoWatchlist(db, NOW);
    const broker = createDemoBroker({ now: () => NOW, ordersEnabled: false });
    const account = await broker.getAccount();
    const history = await broker.getPortfolioHistory();
    assert.equal(history.equity[0], DEPOSIT_USD);
    assert.equal(history.equity.at(-1), account.equity);
    const analytics = buildJournalAnalytics({
        account, positions: await broker.getPositions(), fills: await broker.getActivities('FILL'),
        activities: await broker.getActivities('CSD,CSW'), history, now: NOW,
    });
    assert.equal(analytics.trades.closed, 6);
    assert.equal(analytics.trades.open, 3);
    assert.ok(analytics.trades.win_rate_pct > 0);
    assert.notEqual(analytics.performance.ytd_pnl_usd, 0);
    assert.ok(Math.abs(analytics.capital.current_return_pct - (account.equity / DEPOSIT_USD - 1) * 100) < 1e-9);
});

test('the Algo Universe and the broker quote one symbol at one price', async () => {
    const broker = createDemoBroker({ now: () => NOW, ordersEnabled: false });
    const { quotes } = await broker.getLatestQuotes(['AAPL', 'NVDA']);
    for (const symbol of ['AAPL', 'NVDA']) {
        const row = demoData.universePayload(NOW).symbols.find(item => item.symbol === symbol);
        const midpoint = (quotes[symbol].bp + quotes[symbol].ap) / 2;
        assert.ok(Math.abs(row.quote.midpoint - midpoint) < 0.001, `${symbol}: universe ${row.quote.midpoint} vs quote ${midpoint}`);
    }
});

test('demo intraday bars replay a real session: stable for the day and closing on the live quote', { skip: !require('node:fs').existsSync(require('node:path').join(__dirname, '..', 'server', 'demo-sessions.json')) && 'needs server/demo-sessions.json from scripts/market/export_demo_sessions.py' }, async () => {
    const broker = createDemoBroker({ now: () => NOW, ordersEnabled: false });
    const start = new Date('2026-10-30T08:00:00Z');
    const first = await broker.getBars('AAPL', { timeframe: '5Min', start, end: NOW, limit: 500 });
    const again = await broker.getBars('AAPL', { timeframe: '5Min', start, end: NOW, limit: 500 });
    assert.deepEqual(first.bars, again.bars);
    assert.equal(first.bars.length, 25);
    const closes = first.bars.map(bar => bar.c);
    assert.ok(new Set(closes).size > 10, 'bars should move, not repeat one value');
    const { quotes } = await broker.getLatestQuotes(['AAPL']);
    const mid = (quotes.AAPL.bp + quotes.AAPL.ap) / 2;
    assert.ok(Math.abs(closes.at(-1) - mid) / mid < 0.002, `last close ${closes.at(-1)} should be near the quote ${mid}`);
});

test('the Algo Universe and the dashboard show the same real history for a name', async () => {
    const demoData = require('../server/demo-data');
    const broker = createDemoBroker({ now: () => NOW, ordersEnabled: false });
    const bars = await broker.getBars('AAPL', { timeframe: '5Min', start: new Date('2026-10-30T08:00:00Z'), end: NOW, limit: 500 });
    const universe = demoData.universeHistory('AAPL', '1d', NOW);
    const closes = new Set(bars.bars.map(bar => bar.c));
    assert.ok(universe.points.length > 20);
    assert.ok(universe.points.every(point => closes.has(point.price)), 'universe closes should match the dashboard closes');
});
