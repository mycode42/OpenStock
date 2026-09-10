#!/usr/bin/env node
'use strict';

require('dotenv').config({ path: require('path').join(__dirname, '..', '.env'), quiet: true });
const OpenStockDB = require('../database/db');
const { initDatabase } = require('../database/init');
const { calculateDashboard } = require('../lib/analytics');

initDatabase({ silent: true });
const db = new OpenStockDB(process.env.DATABASE_PATH || '/home/ubuntu/openstock/database/openstock.db');
try {
    const analytics = calculateDashboard({
        stocks: db.getAllStocks(),
        trades: db.getAllTrades(),
        capitalEvents: db.getCapitalEvents(),
        snapshots: db.getEquitySnapshots()
    });
    if (!analytics.equity_available) {
        process.exitCode = 0;
    } else {
        const snapshotAt = new Date().toISOString().replace('T', ' ').replace(/\.\d{3}Z$/, '');
        db.addEquitySnapshot({
            snapshot_at: snapshotAt,
            cash_eur: analytics.capital.available_cash_eur,
            position_value_eur: analytics.capital.invested_eur,
            equity_eur: analytics.capital.current_eur,
            net_contributions_eur: analytics.capital.net_contributions_eur || 0,
            financing_eur: analytics.capital.financing_eur || 0,
            source: 'monitor'
        });
        console.log(`Equity snapshot stored: €${analytics.capital.current_eur.toFixed(2)}`);
    }
} finally {
    db.db.close();
}
