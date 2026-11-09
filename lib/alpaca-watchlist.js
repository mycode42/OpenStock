'use strict';

// Add, retire, restore and delete the symbols the Alpaca watchlist monitors.
//
// Each watchlist row is a copy of a legacy `stocks` row (source_stock_id), and its alert levels copy
// the legacy alert levels. A symbol therefore exists in both tables, and both are written here in one
// transaction, so the dashboard and the monitor always see the same symbol.

function rawDatabase(db) {
    return db?.db || db;
}

function watchlistError(message, status = 400) {
    const error = new Error(message);
    error.status = status;
    return error;
}

function watchlistRow(raw, id) {
    const row = raw.prepare('SELECT * FROM alpaca_watchlist WHERE id = ?').get(id);
    if (!row) throw watchlistError('Alpaca symbol not found', 404);
    return row;
}

// Copies the legacy alert levels of one stock into the watchlist, keeping their state and history.
function copyAlertLevels(raw, watchlistId, sourceStockId, rate = 1) {
    const select = raw.prepare("SELECT * FROM alert_levels WHERE stock_id = ? AND side IN ('BUY', 'SELL') ORDER BY id");
    const insert = raw.prepare(`INSERT INTO alpaca_alert_levels
        (watchlist_id, source_alert_level_id, side, price_usd, enabled, active, triggered, retired,
         triggered_at, retired_at, trigger_mode, change_pct, reference_price_usd, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`);
    for (const level of select.all(sourceStockId)) {
        insert.run(
            watchlistId, level.id, level.side, Number(level.price_eur) * rate,
            level.enabled, level.active, level.triggered, level.retired,
            level.triggered_at, level.retired_at, level.trigger_mode, level.change_pct,
            level.reference_price_eur === null ? null : Number(level.reference_price_eur) * rate,
            level.created_at, level.updated_at,
        );
    }
}

// Adds a USD symbol to the monitored watchlist with a buy and a sell threshold.
// A legacy stock left behind by an earlier failed add is adopted, so the symbol can be added again.
function addSymbol(db, { symbol, name, buy_threshold_usd, sell_threshold_usd }) {
    const raw = rawDatabase(db);
    const onWatchlist = raw.prepare('SELECT id, archived FROM alpaca_watchlist WHERE symbol = ?').get(symbol);
    if (onWatchlist) {
        throw watchlistError(onWatchlist.archived
            ? 'This symbol is retired. Restore it from the retired list instead.'
            : 'Stock already exists', 409);
    }
    return raw.transaction(() => {
        let legacy = raw.prepare('SELECT id FROM stocks WHERE symbol = ?').get(symbol);
        if (legacy) {
            // Adopt the leftover row and move its thresholds to the requested ones.
            raw.prepare(`UPDATE stocks SET name = ?, currency = 'USD', buy_threshold_eur = ?, sell_threshold_eur = ?,
                archived = 0, enabled = 1, updated_at = datetime('now') WHERE id = ?`)
                .run(name, buy_threshold_usd, sell_threshold_usd, legacy.id);
            raw.prepare("UPDATE alert_levels SET price_eur = ? WHERE stock_id = ? AND side = 'BUY' AND retired = 0").run(buy_threshold_usd, legacy.id);
            raw.prepare("UPDATE alert_levels SET price_eur = ? WHERE stock_id = ? AND side = 'SELL' AND retired = 0").run(sell_threshold_usd, legacy.id);
            raw.prepare('DELETE FROM alpaca_alert_levels WHERE watchlist_id IN (SELECT id FROM alpaca_watchlist WHERE source_stock_id = ?)').run(legacy.id);
        } else {
            const created = raw.prepare(`INSERT INTO stocks (symbol, name, isin, currency, buy_threshold_eur, sell_threshold_eur)
                VALUES (?, ?, NULL, 'USD', ?, ?)`).run(symbol, name, buy_threshold_usd, sell_threshold_usd);
            if (raw.prepare("SELECT 1 FROM pragma_table_info('stocks') WHERE name='price_source'").get()) {
                raw.prepare("UPDATE stocks SET price_source='alpaca' WHERE id = ?").run(created.lastInsertRowid);
            }
            const add = raw.prepare('INSERT INTO alert_levels (stock_id, side, price_eur) VALUES (?, ?, ?)');
            add.run(created.lastInsertRowid, 'BUY', buy_threshold_usd);
            add.run(created.lastInsertRowid, 'SELL', sell_threshold_usd);
            legacy = { id: Number(created.lastInsertRowid) };
        }
        const copied = raw.prepare(`INSERT INTO alpaca_watchlist
            (symbol, name, source_stock_id, enabled, archived, copied_at, copied_fx_rate)
            VALUES (?, ?, ?, 1, 0, ?, 1)`).run(symbol, name, legacy.id, new Date().toISOString());
        copyAlertLevels(raw, Number(copied.lastInsertRowid), legacy.id, 1);
        return { id: Number(copied.lastInsertRowid), symbol, name, source_stock_id: legacy.id };
    }).immediate();
}

// Stops monitoring a symbol. Its levels and history stay, so it can be restored.
function retireSymbol(db, id) {
    const raw = rawDatabase(db);
    const row = watchlistRow(raw, id);
    raw.transaction(() => {
        raw.prepare('UPDATE alpaca_watchlist SET enabled = 0, archived = 1 WHERE id = ?').run(id);
        raw.prepare("UPDATE stocks SET enabled = 0, archived = 1, updated_at = datetime('now') WHERE id = ?").run(row.source_stock_id);
    }).immediate();
    return { id, symbol: row.symbol, archived: true };
}

function restoreSymbol(db, id) {
    const raw = rawDatabase(db);
    const row = watchlistRow(raw, id);
    raw.transaction(() => {
        raw.prepare('UPDATE alpaca_watchlist SET enabled = 1, archived = 0 WHERE id = ?').run(id);
        raw.prepare("UPDATE stocks SET enabled = 1, archived = 0, updated_at = datetime('now') WHERE id = ?").run(row.source_stock_id);
    }).immediate();
    return { id, symbol: row.symbol, archived: false };
}

// Removes a symbol and its levels. A symbol with orders, alert deliveries or trades keeps its history,
// so it can only be retired.
function deleteSymbol(db, id) {
    const raw = rawDatabase(db);
    const row = watchlistRow(raw, id);
    const history = raw.prepare(`SELECT
            (SELECT COUNT(*) FROM alpaca_orders o JOIN alpaca_alert_levels l ON l.id = o.alpaca_alert_level_id WHERE l.watchlist_id = ?) AS orders,
            (SELECT COUNT(*) FROM alpaca_alert_outbox o JOIN alpaca_alert_levels l ON l.id = o.alpaca_alert_level_id WHERE l.watchlist_id = ?) AS deliveries,
            (SELECT COUNT(*) FROM trades WHERE stock_id = ?) AS trades`).get(id, id, row.source_stock_id);
    if (history.orders || history.deliveries || history.trades) {
        throw watchlistError('This symbol has order or trade history, so it can only be retired.', 409);
    }
    raw.transaction(() => {
        raw.prepare('DELETE FROM alpaca_watchlist WHERE id = ?').run(id);
        raw.prepare('DELETE FROM stocks WHERE id = ?').run(row.source_stock_id);
    }).immediate();
    return { id, symbol: row.symbol, deleted: true };
}

// Symbols that are retired, for the configure page's retired list.
function retiredSymbols(db) {
    return rawDatabase(db).prepare('SELECT id, symbol, name FROM alpaca_watchlist WHERE archived = 1 ORDER BY symbol').all();
}

module.exports = { addSymbol, deleteSymbol, restoreSymbol, retireSymbol, retiredSymbols, copyAlertLevels };
