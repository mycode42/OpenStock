#!/usr/bin/env node
require('dotenv').config({ path: '/opt/openstock/.env' });
const OpenStockDB = require('../database/db');
const { initDatabase } = require('../database/init');
const { backfillStocks } = require('../lib/history-backfill');
const { TradeRepublicClient } = require('../lib/brokers/trade-republic');

async function main() {
    const dbPath = process.env.DATABASE_PATH || '/opt/openstock/database/openstock.db';
    initDatabase();
    const db = new OpenStockDB(dbPath);
    try {
        await backfillStocks({ db, brokerClient: new TradeRepublicClient({ timeoutMs: Number(process.env.TRADE_REPUBLIC_TIMEOUT_MS || 30000) }), range: process.env.HISTORY_RANGE || '5y', delayMs: process.env.HISTORY_BACKFILL_DELAY_MS || 1500 });
    } finally {
        db.db.close();
    }
}

if (require.main === module) main().catch(error => { console.error(error.message); process.exitCode = 1; });
module.exports = { main };
