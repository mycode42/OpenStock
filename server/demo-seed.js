'use strict';

// Demo watchlist, holdings and alert levels. The prices are illustrative and never reach a broker.
// The demo runs the real Alpaca code paths against this data: a simulated broker stands in for Alpaca.
const { addPriceLevel, setOrderSettings } = require('../lib/alpaca-alert-levels');

const DEPOSIT_USD = 100_000;

const WATCHLIST = Object.freeze([
    { symbol: 'AAPL', name: 'Apple Inc.', base: 219.42, held: 8, buyIn: 205.1 },
    { symbol: 'NVDA', name: 'NVIDIA Corporation', base: 132.86, held: 12, buyIn: 118.35 },
    { symbol: 'AMD', name: 'Advanced Micro Devices', base: 163.57, held: 5, buyIn: 150.2 },
    { symbol: 'MSFT', name: 'Microsoft Corporation', base: 521.7, held: 0, buyIn: 0 },
    { symbol: 'TSLA', name: 'Tesla, Inc.', base: 393.28, held: 0, buyIn: 0 },
    { symbol: 'AMZN', name: 'Amazon.com, Inc.', base: 226.42, held: 0, buyIn: 0 },
]);

// Levels to create. `retired` marks a level that has already fired, so the Triggered section has a row.
const LEVELS = Object.freeze([
    { symbol: 'AAPL', side: 'BUY', price_usd: 206, auto_order: true, budget_usd: 500 },
    { symbol: 'AAPL', side: 'SELL', price_usd: 237 },
    { symbol: 'NVDA', side: 'BUY', price_usd: 124 },
    { symbol: 'NVDA', side: 'SELL', price_usd: 143 },
    { symbol: 'AMD', side: 'SELL', price_usd: 180 },
    { symbol: 'MSFT', side: 'BUY', price_usd: 505 },
    { symbol: 'MSFT', side: 'BUY', price_usd: 530, retired: true },
    { symbol: 'TSLA', side: 'BUY', price_usd: 380 },
    // Above the market, so an order at this level fills straight away.
    { symbol: 'TSLA', side: 'BUY', price_usd: 396 },
]);

function seedDemoWatchlist(db, now = new Date()) {
    const count = db.prepare('SELECT COUNT(*) AS count FROM alpaca_watchlist').get().count;
    if (count > 0) return false;
    // Each watchlist row is a copy of a legacy stock row, so the seed creates that row first (or reuses it).
    const findSource = db.prepare('SELECT id FROM stocks WHERE symbol = ?');
    const insertSource = db.prepare("INSERT INTO stocks (symbol, name, currency, buy_threshold_eur, sell_threshold_eur) VALUES (?, ?, 'USD', 0, 0)");
    const insertStock = db.prepare('INSERT INTO alpaca_watchlist (symbol, name, source_stock_id, enabled, archived, copied_fx_rate) VALUES (?, ?, ?, 1, 0, 1)');
    const ids = new Map();
    for (const stock of WATCHLIST) {
        const source = findSource.get(stock.symbol) || { id: Number(insertSource.run(stock.symbol, stock.name).lastInsertRowid) };
        ids.set(stock.symbol, Number(insertStock.run(stock.symbol, stock.name, source.id).lastInsertRowid));
    }
    for (const level of LEVELS) {
        const created = addPriceLevel(db, ids.get(level.symbol), { side: level.side, price_usd: level.price_usd });
        if (level.auto_order) setOrderSettings(db, created.id, { auto_order: true, budget_usd: level.budget_usd });
        if (level.retired) {
            const at = now.toISOString();
            db.prepare(`UPDATE alpaca_alert_levels SET triggered=1, retired=1, active=0, triggered_at=?, retired_at=?
                WHERE id=?`).run(at, at, created.id);
        }
    }
    return true;
}

module.exports = { DEPOSIT_USD, LEVELS, WATCHLIST, seedDemoWatchlist };
