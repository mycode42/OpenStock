#!/usr/bin/env node
/* Research-only Yahoo backfill. Run manually once to seed local dashboard charts.
 * Normal dashboard hovers never make Yahoo requests; all later points come from
 * the one-minute monitor snapshots. */
require('dotenv').config({ path: '/opt/openstock/.env' });
const OpenStockDB = require('../database/db');
const { initDatabase } = require('../database/init');

const dbPath = process.env.DATABASE_PATH || '/opt/openstock/database/openstock.db';
const historyRange = process.env.HISTORY_RANGE || '1mo';
if (!['1mo', '1y', '5y'].includes(historyRange)) {
    throw new Error('HISTORY_RANGE must be one of: 1mo, 1y, 5y');
}
initDatabase();
const db = new OpenStockDB(dbPath);

function sqliteTimestamp(unixSeconds) {
    return new Date(unixSeconds * 1000).toISOString().replace('T', ' ').replace(/\.\d{3}Z$/, '');
}

async function backfill(stock) {
    const response = await fetch(`https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(stock.symbol)}?range=${historyRange}&interval=1d&events=history`, {
        headers: { 'User-Agent': 'Mozilla/5.0 OpenStock research history', Accept: 'application/json', 'Cache-Control': 'no-cache' }
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const chart = (await response.json()).chart?.result?.[0];
    const timestamps = chart?.timestamp || [];
    const closes = chart?.indicators?.quote?.[0]?.close || [];
    let saved = 0;
    for (let index = 0; index < timestamps.length; index++) {
        const price = Number(closes[index]);
        if (!Number.isFinite(price) || price <= 0) continue;
        const recordedAt = sqliteTimestamp(timestamps[index]);
        db.addPriceSnapshot(stock.id, price, null, null, 'yahoo_backfill', recordedAt, recordedAt);
        saved++;
    }
    return saved;
}

(async () => {
    let total = 0;
    for (const stock of db.getAllStocks()) {
        try {
            const saved = await backfill(stock);
            total += saved;
            console.log(`${stock.symbol}: ${saved} daily points`);
        } catch (error) {
            console.error(`${stock.symbol}: ${error.message}`);
        }
    }
    console.log(`${historyRange} backfill complete: ${total} points`);
    db.db.close();
})().catch(error => { console.error(error.message); db.db.close(); process.exit(1); });
