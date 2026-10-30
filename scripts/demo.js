#!/usr/bin/env node
'use strict';

// Starts OpenStock in demo mode: simulated quotes, no broker login, no Algo Trader data.
// Credential and config variables are overridden so a local .env is never read.
const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { initDatabase } = require('../database/init');
const OpenStockDB = require('../database/db');
const { seedDemoWatchlist } = require('../server/demo-seed');

const appDir = path.join(__dirname, '..');
const demoDir = path.join(appDir, 'data', 'demo');
fs.mkdirSync(demoDir, { recursive: true });

const env = {
    ...process.env,
    // Deliberately absent: keeps the config loader from reading a real .env.
    OPENSTOCK_CONFIG_FILE: path.join(demoDir, 'no-config.env'),
    DATABASE_PATH: path.join(demoDir, 'openstock.db'),
    ALGO_TRADER_ROOT: path.join(demoDir, 'no-algo-trader'),
    // Demo can never trade: blank every broker URL, live switch and order token as well as the keys.
    ALPACA_MANUAL_BASE_URL: '',
    ALPACA_MANUAL_API_KEY: '',
    ALPACA_MANUAL_API_SECRET: '',
    ALPACA_EQUITY_BASE_URL: '',
    ALPACA_OPTIONS_BASE_URL: '',
    ALPACA_ALLOW_LIVE: '',
    OPENSTOCK_ORDER_TOKEN: '',
    ALPACA_EQUITY_API_KEY: '',
    ALPACA_EQUITY_API_SECRET: '',
    ALPACA_EQUITY_ACCOUNT_ID: '',
    ALPACA_OPTIONS_API_KEY: '',
    ALPACA_OPTIONS_API_SECRET: '',
    ALPACA_OPTIONS_ACCOUNT_ID: '',
    // Simulated orders are accepted only in this database, never in a real one.
    OPENSTOCK_DEMO_DATABASE: '1',
};

// Sample journal rows go into the demo database only; seeding is skipped once it has trades.
initDatabase({ dbPath: env.DATABASE_PATH, silent: true });
const demoDb = new OpenStockDB(env.DATABASE_PATH);
seedDemoWatchlist(demoDb.db);
demoDb.db.close();

const port = env.PORT || 3000;
process.stdout.write(`OpenStock demo mode: simulated data on http://127.0.0.1:${port}\n`);

const child = spawn(process.execPath, [path.join(appDir, 'server.js')], { stdio: 'inherit', env });
for (const signal of ['SIGINT', 'SIGTERM']) {
    process.on(signal, () => child.kill(signal));
}
child.on('exit', (code, signal) => process.exit(signal ? 1 : code ?? 0));
