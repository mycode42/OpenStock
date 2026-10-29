'use strict';

const { randomBytes } = require('node:crypto');
const { makeQuote, AlpacaError } = require('./brokers/alpaca');

const PAPER_BASE = 'https://paper-api.alpaca.markets/v2';
// Largest notional value of one order. Live and paper alike.
const MAX_ORDER_USD = 100000;
// Most live notional that may be placed in one UTC day. Paper orders do not count.
const DAILY_LIVE_LIMIT_USD = 300000;
const LIVE_OPT_IN = 'ALPACA_ALLOW_LIVE';
const ACTIVE_STATUSES = ['SUBMITTING', 'NEW', 'PENDING_CANCEL', 'PARTIALLY_FILLED'];
const STATUS_FROM_ALPACA = {
    new: 'NEW', accepted: 'NEW', pending_new: 'NEW', accepted_for_bidding: 'NEW', held: 'NEW',
    pending_replace: 'NEW', partially_filled: 'PARTIALLY_FILLED', filled: 'FILLED',
    pending_cancel: 'PENDING_CANCEL', canceled: 'CANCELED', replaced: 'CANCELED',
    done_for_day: 'EXPIRED', expired: 'EXPIRED', stopped: 'EXPIRED', rejected: 'REJECTED',
};
// A submit that Alpaca never acknowledged is marked FAILED after this long.
const UNCONFIRMED_SUBMIT_MS = 5 * 60_000;

function orderError(message, status) {
    const error = new Error(message);
    error.status = status;
    return error;
}

function rawDatabase(db) {
    return db?.db || db;
}

function orderSettings(env = process.env) {
    const positive = (name, fallback) => {
        const value = Number(env[name]);
        return Number.isFinite(value) && value > 0 ? value : fallback;
    };
    const mode = tradingMode(env);
    return {
        mode,
        live: mode === 'live',
        liveAllowed: String(env[LIVE_OPT_IN] || '').trim() === '1',
        token: String(env.OPENSTOCK_ORDER_TOKEN || '').trim(),
        notionalUsd: positive('ORDER_NOTIONAL_USD', 250),
    };
}

// Paper unless ALPACA_MANUAL_BASE_URL explicitly points elsewhere.
function tradingMode(env = process.env) {
    const base = String(env.ALPACA_MANUAL_BASE_URL || PAPER_BASE).trim().replace(/\/$/, '');
    return base === PAPER_BASE ? 'paper' : 'live';
}

function priceTick(price) {
    return price < 1 ? 4 : 2;
}

function roundPrice(price) {
    return Number(price.toFixed(priceTick(price)));
}

function publicSettings(env = process.env) {
    const settings = orderSettings(env);
    return {
        mode: settings.mode,
        live_allowed: settings.liveAllowed,
        max_order_usd: MAX_ORDER_USD,
        notional_usd: settings.notionalUsd,
    };
}

// Builds the order for a level. Shared by dry runs and real submits.
async function planLevelOrder({ db, client, levelId, env = process.env, amountUsd, automatic = false }) {
    const settings = orderSettings(env);
    // A live account needs an explicit opt-in on top of the order token the routes check.
    if (settings.live && !settings.liveAllowed) {
        throw orderError('Live trading is off: set ALPACA_ALLOW_LIVE=1 to place orders on the live account', 403);
    }
    const level = db.prepare(`SELECT l.*, w.symbol FROM alpaca_alert_levels l
        JOIN alpaca_watchlist w ON w.id = l.watchlist_id WHERE l.id=?`).get(levelId);
    if (!level) throw orderError('Alpaca alert level not found', 404);
    // The monitor retires a level in the same step that it fires it, so the automatic path skips this guard.
    if (!automatic) {
        if (!level.enabled || !level.active || level.retired) throw orderError('Alert level is not active', 409);
        if (level.triggered) throw orderError('Alert level has already triggered', 409);
    }

    const price = roundPrice(Number(level.price_usd));
    if (!(price > 0)) throw orderError('Alert level has no usable price', 409);

    // A limit never pays more than its price, so a limit that is already
    // marketable fills at the better market price and cannot overpay.
    const symbol = level.symbol;
    let qty;
    if (level.side === 'BUY') {
        const amount = amountUsd ?? settings.notionalUsd;
        qty = Math.floor(amount / price);
        if (qty < 1) throw orderError(`Amount $${amount} buys less than one share at $${price}`, 400);
    } else {
        // Sells exit the whole open position.
        const positions = await client.getPositions();
        const position = (positions || []).find(row => String(row.symbol).toUpperCase() === symbol);
        qty = Math.floor(Math.abs(Number(position?.qty || 0)));
        if (qty < 1) throw orderError('No open Alpaca position to sell', 409);
    }

    const value = qty * price;
    if (value > MAX_ORDER_USD) {
        throw orderError('Order value exceeds the $100,000 per-order limit', 400);
    }
    return {
        alpaca_alert_level_id: level.id,
        symbol,
        side: level.side,
        qty,
        limit_price_usd: price,
        notional_usd: Number((qty * price).toFixed(2)),
        time_in_force: 'day',
        mode: settings.mode,
    };
}

function orderRow(db, id) {
    return db.prepare('SELECT * FROM alpaca_orders WHERE id=?').get(id);
}

// Called by the monitor right after a level with auto_order fires. Same gates as manual orders.
async function placeTriggeredOrder({ db, client, levelId, env = process.env, now = () => new Date() }) {
    db = rawDatabase(db);
    const settings = orderSettings(env);
    const level = db.prepare('SELECT auto_order, side, order_budget_usd FROM alpaca_alert_levels WHERE id=?').get(levelId);
    if (!level?.auto_order) return { skipped: 'Alert level is not set to place real orders' };
    if (level.side === 'BUY' && !(Number(level.order_budget_usd) > 0)) return { skipped: 'BUY level has no order budget' };
    // BUY spends the level's own budget; SELL ignores the amount and exits the position.
    const amountUsd = level.side === 'BUY' ? Number(level.order_budget_usd) : undefined;
    return placeLevelOrder({ db, client, levelId, env, now, amountUsd, automatic: true });
}

async function placeLevelOrder({ db, client, levelId, env = process.env, dryRun = false, amountUsd, automatic = false, now = () => new Date() }) {
    db = rawDatabase(db);
    const plan = await planLevelOrder({ db, client, levelId, env, amountUsd, automatic });
    if (dryRun) return { plan };

    const clientOrderId = `os-${levelId}-${now().getTime().toString(36)}-${randomBytes(3).toString('hex')}`;
    let rowId;
    // The daily check and the insert run in one synchronous transaction, so concurrent placements cannot both pass it.
    const insertOrder = db.transaction(() => {
        if (plan.mode === 'live') {
            const used = db.prepare(`SELECT COALESCE(SUM(notional_usd), 0) AS used FROM alpaca_orders
                WHERE mode='live' AND status NOT IN ('REJECTED','FAILED') AND date(created_at)=date('now')`).get().used;
            if (used + plan.notional_usd > DAILY_LIVE_LIMIT_USD) {
                throw orderError('Live daily limit reached: no more than $300,000 of live orders per UTC day', 400);
            }
        }
        return db.prepare(`INSERT INTO alpaca_orders (alpaca_alert_level_id, client_order_id, symbol, side, qty,
            limit_price_usd, notional_usd, time_in_force, status, mode) VALUES (?,?,?,?,?,?,?,?, 'SUBMITTING', ?)`)
            .run(plan.alpaca_alert_level_id, clientOrderId, plan.symbol, plan.side, plan.qty,
                plan.limit_price_usd, plan.notional_usd, plan.time_in_force, plan.mode).lastInsertRowid;
    });
    try {
        rowId = insertOrder();
    } catch (error) {
        if (String(error.code).startsWith('SQLITE_CONSTRAINT')) throw orderError('An order is already open for this alert level', 409);
        throw error;
    }

    const body = {
        symbol: plan.symbol,
        qty: String(plan.qty),
        side: plan.side.toLowerCase(),
        type: 'limit',
        time_in_force: plan.time_in_force,
        limit_price: plan.limit_price_usd.toFixed(priceTick(plan.limit_price_usd)),
        client_order_id: clientOrderId,
        // Day limit orders run in pre-market, regular and after-hours sessions.
        extended_hours: true,
    };
    try {
        const response = await client.submitOrder(body);
        db.prepare(`UPDATE alpaca_orders SET broker_order_id=?, status=?, submitted_at=?, updated_at=datetime('now') WHERE id=?`)
            .run(response.id, STATUS_FROM_ALPACA[response.status] || 'NEW', now().toISOString(), rowId);
    } catch (error) {
        if (error instanceof AlpacaError && error.status >= 400 && error.status < 500) {
            db.prepare(`UPDATE alpaca_orders SET status='REJECTED', last_error=?, updated_at=datetime('now') WHERE id=?`)
                .run(error.message, rowId);
        } else {
            // Outcome unknown: leave SUBMITTING so the sync looks it up by client_order_id.
            db.prepare(`UPDATE alpaca_orders SET last_error=?, updated_at=datetime('now') WHERE id=?`)
                .run(error.message, rowId);
        }
    }
    return { order: orderRow(db, rowId) };
}

async function cancelOpenOrder({ db, client, orderId }) {
    db = rawDatabase(db);
    const row = orderRow(db, orderId);
    if (!row) throw orderError('Order not found', 404);
    if (!ACTIVE_STATUSES.includes(row.status)) throw orderError(`Order is ${row.status.toLowerCase()}`, 409);
    if (!row.broker_order_id) throw orderError('Order is not yet confirmed by Alpaca; try again shortly', 409);
    await client.cancelOrder(row.broker_order_id);
    db.prepare(`UPDATE alpaca_orders SET status='PENDING_CANCEL', updated_at=datetime('now') WHERE id=?`).run(orderId);
    return { order: orderRow(db, orderId) };
}

// Pulls status for every live order. Runs inside the monitor cycle.
async function syncOpenOrders({ db, client, now = () => new Date() }) {
    db = rawDatabase(db);
    const open = db.prepare(`SELECT * FROM alpaca_orders WHERE status IN (${ACTIVE_STATUSES.map(() => '?').join(',')})`)
        .all(...ACTIVE_STATUSES);
    let updated = 0;
    for (const row of open) {
        try {
            const payload = row.broker_order_id
                ? await client.getOrder(row.broker_order_id)
                : await client.getOrderByClientId(row.client_order_id);
            const status = STATUS_FROM_ALPACA[payload.status] || row.status;
            db.prepare(`UPDATE alpaca_orders SET broker_order_id=?, status=?, filled_qty=?, filled_avg_price_usd=?,
                last_error=NULL, updated_at=datetime('now') WHERE id=?`)
                .run(payload.id, status, Number(payload.filled_qty || 0),
                    payload.filled_avg_price == null ? null : Number(payload.filled_avg_price), row.id);
            updated += 1;
        } catch (error) {
            const age = now().getTime() - Date.parse(`${row.created_at.replace(' ', 'T')}Z`);
            if (!row.broker_order_id && error.status === 404 && age > UNCONFIRMED_SUBMIT_MS) {
                db.prepare(`UPDATE alpaca_orders SET status='FAILED', last_error=?, updated_at=datetime('now') WHERE id=?`)
                    .run('Alpaca never acknowledged this order', row.id);
            } else {
                db.prepare(`UPDATE alpaca_orders SET last_error=?, updated_at=datetime('now') WHERE id=?`).run(error.message, row.id);
            }
        }
    }
    return { checked: open.length, updated };
}

function listOrders(db, { limit = 50 } = {}) {
    return rawDatabase(db).prepare(`SELECT o.*, w.symbol AS watch_symbol FROM alpaca_orders o
        JOIN alpaca_alert_levels l ON l.id = o.alpaca_alert_level_id
        JOIN alpaca_watchlist w ON w.id = l.watchlist_id
        ORDER BY o.id DESC LIMIT ?`).all(Math.min(Math.max(Number(limit) || 50, 1), 200));
}

function openOrdersByLevel(db) {
    const rows = rawDatabase(db).prepare(`SELECT * FROM alpaca_orders WHERE status IN (${ACTIVE_STATUSES.map(() => '?').join(',')})`)
        .all(...ACTIVE_STATUSES);
    return new Map(rows.map(row => [row.alpaca_alert_level_id, row]));
}

module.exports = {
    ACTIVE_STATUSES,
    MAX_ORDER_USD,
    cancelOpenOrder,
    listOrders,
    openOrdersByLevel,
    orderSettings,
    placeLevelOrder,
    placeTriggeredOrder,
    publicSettings,
    syncOpenOrders,
    tradingMode,
};
