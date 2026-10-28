#!/usr/bin/env node
'use strict';

require('dotenv').config({ path: require('../lib/paths').configFilePath(), quiet: true });
const { initDatabase } = require('../database/init');
const OpenStockDB = require('../database/db');
const paths = require('../lib/paths');
const { AlpacaClient } = require('../lib/brokers/alpaca');

const MIGRATION_NAME = 'copy_enabled_watchlist_to_alpaca_usd_v1';

async function currentEurUsdRate(fetchImpl = globalThis.fetch) {
    const response = await fetchImpl('https://api.frankfurter.app/latest?from=EUR&to=USD', {
        signal: AbortSignal.timeout(10000),
        headers: { Accept: 'application/json' }
    });
    if (!response?.ok) throw new Error(`Exchange rate lookup failed (${response?.status || 'unknown status'})`);
    const payload = await response.json();
    const rate = Number(payload?.rates?.USD);
    if (!Number.isFinite(rate) || rate <= 0) throw new Error('Exchange rate lookup returned no EUR/USD rate');
    return rate;
}

async function main() {
    initDatabase({ silent: true, seedDefaults: false });
    const wrapper = new OpenStockDB(paths.databasePath());
    const db = wrapper.db;
    try {
        if (db.prepare('SELECT 1 FROM alpaca_profile_migrations WHERE name=?').get(MIGRATION_NAME)) {
            process.stdout.write('Alpaca profile migration already completed; no changes made.\n');
            return;
        }
        if (db.prepare('SELECT COUNT(*) AS count FROM alpaca_watchlist').get().count) {
            throw new Error('Alpaca watchlist already has rows; refusing to replace existing profile data');
        }
        const alpaca = new AlpacaClient();
        if (!alpaca.isConfigured()) throw new Error('Alpaca API credentials are not configured');
        const stocks = db.prepare(`SELECT id, symbol, name FROM stocks
            WHERE enabled=1 AND archived=0 AND asset_type='stock' ORDER BY symbol`).all();
        if (!stocks.length) throw new Error('No enabled stocks were found to copy');
        if (stocks.length > 25) throw new Error('The source watchlist exceeds the 25-name monitoring limit');

        const rate = await currentEurUsdRate();
        for (const stock of stocks) {
            const asset = await alpaca.getAsset(stock.symbol);
            if (asset?.status !== 'active' || asset?.tradable !== true) {
                throw new Error(`Alpaca asset validation failed for ${stock.symbol}`);
            }
        }

        const selectLevels = db.prepare('SELECT * FROM alert_levels WHERE stock_id=? ORDER BY id');
        const insertWatchlist = db.prepare(`INSERT INTO alpaca_watchlist
            (symbol, name, source_stock_id, enabled, archived, copied_at, copied_fx_rate)
            VALUES (?, ?, ?, 1, 0, ?, ?)`);
        const insertLevel = db.prepare(`INSERT INTO alpaca_alert_levels
            (watchlist_id, source_alert_level_id, side, price_usd, enabled, active, triggered, retired,
             triggered_at, retired_at, trigger_mode, change_pct, reference_price_usd, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`);
        let alertCount = 0;
        const copiedAt = new Date().toISOString();
        db.transaction(() => {
            for (const stock of stocks) {
                const watchlist = insertWatchlist.run(stock.symbol, stock.name, stock.id, copiedAt, rate);
                for (const level of selectLevels.all(stock.id)) {
                    insertLevel.run(
                        watchlist.lastInsertRowid,
                        level.id,
                        level.side,
                        Number(level.price_eur) * rate,
                        level.enabled,
                        level.active,
                        level.triggered,
                        level.retired,
                        level.triggered_at,
                        level.retired_at,
                        level.trigger_mode,
                        level.change_pct,
                        level.reference_price_eur === null ? null : Number(level.reference_price_eur) * rate,
                        level.created_at,
                        level.updated_at
                    );
                    alertCount += 1;
                }
            }
            db.prepare(`INSERT INTO alpaca_profile_migrations (name, completed_at, fx_rate, symbol_count, alert_count)
                VALUES (?, ?, ?, ?, ?)`).run(MIGRATION_NAME, copiedAt, rate, stocks.length, alertCount);
        }).immediate();
        process.stdout.write(`Copied ${stocks.length} enabled tickers and ${alertCount} alert levels to the Alpaca USD watchlist at ${rate.toFixed(4)} USD/EUR.\n`);
    } finally {
        db.close();
    }
}

main().catch(error => {
    process.stderr.write(`Alpaca profile migration failed: ${error.message}\n`);
    process.exitCode = 1;
});

module.exports = { currentEurUsdRate };
