#!/usr/bin/env node
'use strict';

require('dotenv').config({ path: require('node:path').join(__dirname, '..', '.env'), quiet: true });
const OpenStockDB = require('../database/db');
const { calculateDashboard } = require('../lib/analytics');
const { buildCashFlowEvents, buildReplaySnapshots } = require('../lib/equity-replay');

const db = new OpenStockDB(require('../lib/paths').databasePath());
try {
    const sql = db.db;
    const trades = sql.prepare('SELECT t.*,s.symbol,s.name,s.asset_type,s.current_price_eur FROM trades t JOIN stocks s ON s.id=t.stock_id ORDER BY t.opened_at,t.id').all();
    const stocks = db.getAllStocks();
    const capitalEvents = db.getCapitalEvents();
    // Trade Republic executions were removed (migration 029); the journal trades carry the cash flows.
    const brokerExecutions = [];
    const mergeEvents = sql.prepare('SELECT target_trade_id,source_trade_id,source_trade_json FROM trade_merge_events ORDER BY id').all();
    const priceHistory = sql.prepare('SELECT stock_id,price_eur,quote_time,recorded_at FROM price_history WHERE price_eur IS NOT NULL ORDER BY stock_id,id').all();
    const firstSnapshot = sql.prepare('SELECT MIN(snapshot_at) AS at FROM equity_snapshots').get()?.at;
    if (!firstSnapshot) throw new Error('No equity snapshots exist to establish the replay start date');
    const now = new Date();
    const events = buildCashFlowEvents({ trades, brokerExecutions, mergeEvents, capitalEvents });
    const points = buildReplaySnapshots({ events, priceHistory, stocks, startAt: firstSnapshot, now });
    const missing = points.flatMap(point => point.missing_prices.map(symbol => `${symbol} at ${point.snapshot_at}`));
    if (missing.length) throw new Error(`Cannot rebuild complete curve; missing historical marks: ${missing.slice(0, 10).join(', ')}`);
    const current = calculateDashboard({ stocks, trades, capitalEvents, now: now.toISOString() });
    const latest = points.at(-1);
    if (!current.equity_available || Math.abs(latest.cash_eur - current.capital.available_cash_eur) > 0.01 || Math.abs(latest.position_value_eur - current.capital.invested_eur) > 0.01) {
        throw new Error('Replay does not reconcile to the current Journal ledger; snapshots were not changed');
    }
    console.log(JSON.stringify({ mode: process.argv.includes('--apply') ? 'apply' : 'dry-run', count: points.length, first: points[0], latest: { snapshot_at: latest.snapshot_at, cash_eur: latest.cash_eur, position_value_eur: latest.position_value_eur, equity_eur: latest.equity_eur, net_contributions_eur: latest.net_contributions_eur }, reconciles_current_ledger: true }, null, 2));
    if (process.argv.includes('--apply')) {
        const columns = sql.prepare('PRAGMA table_info(equity_snapshots)').all().map(column => column.name);
        const hasDecimals = ['cash_eur_decimal','position_value_eur_decimal','equity_eur_decimal','net_contributions_eur_decimal','financing_eur_decimal'].every(column => columns.includes(column));
        const insert = hasDecimals
            ? sql.prepare(`INSERT INTO equity_snapshots (snapshot_at,cash_eur,position_value_eur,equity_eur,net_contributions_eur,financing_eur,source,cash_eur_decimal,position_value_eur_decimal,equity_eur_decimal,net_contributions_eur_decimal,financing_eur_decimal) VALUES (@snapshot_at,@cash_eur,@position_value_eur,@equity_eur,@net_contributions_eur,@financing_eur,'journal_replay',@cash_eur_decimal,@position_value_eur_decimal,@equity_eur_decimal,@net_contributions_eur_decimal,@financing_eur_decimal)`)
            : sql.prepare(`INSERT INTO equity_snapshots (snapshot_at,cash_eur,position_value_eur,equity_eur,net_contributions_eur,financing_eur,source) VALUES (@snapshot_at,@cash_eur,@position_value_eur,@equity_eur,@net_contributions_eur,@financing_eur,'journal_replay')`);
        const rebuild = sql.transaction(() => {
            sql.prepare('DELETE FROM equity_snapshots').run();
            for (const point of points) {
                const row = { ...point, financing_eur: point.financing_eur || 0 };
                if (hasDecimals) for (const key of ['cash_eur','position_value_eur','equity_eur','net_contributions_eur','financing_eur']) row[`${key}_decimal`] = Number(row[key]).toString();
                insert.run(row);
            }
        });
        rebuild();
        console.log(`Rebuilt ${points.length} daily Journal equity snapshots.`);
    }
} finally {
    db.db.close();
}
