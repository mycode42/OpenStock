'use strict';

const PERCENT_LIMIT = { DROP: 100, GAIN: 10000 };

function rawDatabase(db) {
    return db?.db || db;
}

function levelError(message, status = 400) {
    const error = new Error(message);
    error.status = status;
    return error;
}

function levelRow(db, id) {
    return rawDatabase(db).prepare('SELECT * FROM alpaca_alert_levels WHERE id=?').get(id);
}

// Source ids come from the old stock table; new levels take the next unused negative id so the UNIQUE column holds.
function insertLevel(db, watchlistId, fields) {
    const result = rawDatabase(db).prepare(`INSERT INTO alpaca_alert_levels (watchlist_id, source_alert_level_id, side, price_usd,
        trigger_mode, change_pct, reference_price_usd)
        VALUES (?, (SELECT COALESCE(MIN(source_alert_level_id), 0) - 1 FROM alpaca_alert_levels), ?, ?, ?, ?, ?)`)
        .run(watchlistId, fields.side, fields.price_usd, fields.trigger_mode, fields.change_pct, fields.reference_price_usd);
    return levelRow(db, Number(result.lastInsertRowid));
}

function addPriceLevel(db, watchlistId, { side, price_usd }) {
    if (!rawDatabase(db).prepare('SELECT id FROM alpaca_watchlist WHERE id=?').get(watchlistId)) throw levelError('Alpaca symbol not found', 404);
    const normalized = String(side || '').toUpperCase();
    if (!['BUY', 'SELL'].includes(normalized)) throw levelError('Side must be BUY or SELL');
    const price = Number(price_usd);
    if (!(price > 0)) throw levelError('Alert price must be positive');
    return insertLevel(db, watchlistId, { side: normalized, price_usd: price, trigger_mode: 'PRICE', change_pct: null, reference_price_usd: null });
}

// The target is worked out from the reference quote: DROP is a BUY below it, GAIN is a SELL above it.
function addPercentLevel(db, watchlistId, { direction, change_pct, reference_price_usd }) {
    if (!rawDatabase(db).prepare('SELECT id FROM alpaca_watchlist WHERE id=?').get(watchlistId)) throw levelError('Alpaca symbol not found', 404);
    const move = String(direction || '').toUpperCase();
    if (!PERCENT_LIMIT[move]) throw levelError('Direction must be DROP or GAIN');
    const pct = Number(change_pct);
    if (!(pct > 0) || pct >= PERCENT_LIMIT[move]) throw levelError('Percentage must be positive and below the maximum for that direction');
    const reference = Number(reference_price_usd);
    if (!(reference > 0)) throw levelError('A current Alpaca quote is required before adding a percentage alert', 409);
    const amount = reference * pct / 100;
    return insertLevel(db, watchlistId, {
        side: move === 'DROP' ? 'BUY' : 'SELL',
        price_usd: move === 'DROP' ? reference - amount : reference + amount,
        trigger_mode: 'PERCENT', change_pct: pct, reference_price_usd: reference,
    });
}

function booleanField(value, name) {
    if (![0, 1, true, false].includes(value)) throw levelError(`${name} must be boolean`);
    return value ? 1 : 0;
}

// Price, percentage and on/off edits. Real-order settings have their own gated route.
function updateLevel(db, id, body = {}) {
    const existing = levelRow(db, id);
    if (!existing) throw levelError('Alert level not found', 404);
    const updates = {};
    if (body.price_usd !== undefined) {
        if (existing.trigger_mode === 'PERCENT') throw levelError('The target price for a percentage alert is calculated from its reference price');
        const price = Number(body.price_usd);
        if (!(price > 0)) throw levelError('Alert price must be positive');
        updates.price_usd = price;
    }
    if (body.side !== undefined) {
        if (existing.trigger_mode === 'PERCENT') throw levelError('A percentage alert keeps its direction');
        const side = String(body.side).toUpperCase();
        if (!['BUY', 'SELL'].includes(side)) throw levelError('Side must be BUY or SELL');
        updates.side = side;
    }
    if (body.change_pct !== undefined) {
        if (existing.trigger_mode !== 'PERCENT') throw levelError('Only percentage alerts have a change percentage');
        const pct = Number(body.change_pct);
        if (!(pct > 0) || pct >= PERCENT_LIMIT[existing.side === 'BUY' ? 'DROP' : 'GAIN']) throw levelError('Percentage must be positive and below the maximum for that direction');
        const amount = Number(existing.reference_price_usd) * pct / 100;
        updates.change_pct = pct;
        updates.price_usd = existing.side === 'BUY' ? Number(existing.reference_price_usd) - amount : Number(existing.reference_price_usd) + amount;
    }
    if (body.enabled !== undefined) updates.enabled = booleanField(body.enabled, 'enabled');
    if (body.active !== undefined) updates.active = booleanField(body.active, 'active');
    if (updates.enabled === 0) updates.active = 0;
    if (updates.enabled === 1 && body.active === undefined) updates.active = 1;
    if ((existing.triggered || existing.retired) && (updates.enabled === 1 || updates.active === 1)) {
        throw levelError('Retired alert levels must be rearmed explicitly');
    }
    const entries = Object.entries(updates);
    if (!entries.length) throw levelError('No valid alert level fields to update');
    rawDatabase(db).prepare(`UPDATE alpaca_alert_levels SET ${entries.map(([key]) => `${key}=?`).join(', ')}, updated_at=datetime('now') WHERE id=?`)
        .run(...entries.map(([, value]) => value), id);
    return levelRow(db, id);
}

// Real-order switch. A BUY needs a USD budget; a SELL exits the whole position and takes none.
function setOrderSettings(db, id, { auto_order, budget_usd }) {
    const existing = levelRow(db, id);
    if (!existing) throw levelError('Alpaca alert level not found', 404);
    if (typeof auto_order !== 'boolean') throw levelError('auto_order must be true or false');
    let budget = existing.order_budget_usd;
    if (budget_usd !== undefined) {
        budget = Number(budget_usd);
        if (!(budget > 0)) throw levelError('Order budget must be a positive USD amount');
    }
    if (auto_order && existing.side === 'BUY' && !(budget > 0)) throw levelError('A BUY level needs an order budget in USD');
    rawDatabase(db).prepare(`UPDATE alpaca_alert_levels SET auto_order=?, order_budget_usd=?, updated_at=datetime('now') WHERE id=?`)
        .run(auto_order ? 1 : 0, budget ?? null, id);
    return levelRow(db, id);
}

function deleteLevel(db, id) {
    const raw = rawDatabase(db);
    const existing = levelRow(db, id);
    if (!existing) throw levelError('Alert level not found', 404);
    const open = raw.prepare(`SELECT id FROM alpaca_orders WHERE alpaca_alert_level_id=?
        AND status IN ('SUBMITTING','NEW','PENDING_CANCEL','PARTIALLY_FILLED')`).get(id);
    if (open) throw levelError('Cancel the open order for this level before deleting it', 409);
    raw.prepare('DELETE FROM alpaca_alert_levels WHERE id=?').run(id);
    return { deleted_id: id };
}

module.exports = { addPercentLevel, addPriceLevel, deleteLevel, setOrderSettings, updateLevel };
