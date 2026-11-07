#!/usr/bin/env node
'use strict';

require('dotenv').config({ path: require('node:path').join(__dirname, '..', '.env'), quiet: true });
const fs = require('node:fs');
const path = require('node:path');
const Database = require('better-sqlite3');
const { initDatabase } = require('../database/init');
const { AlpacaClient } = require('../lib/brokers/alpaca');
const { EXIT, safeError } = require('../lib/monitor');
const { monitorPaths, createMonitorLogger, runMonitorCycle } = require('../lib/monitor-cycle');

if (process.argv.includes('--help')) {
    process.stdout.write('Usage: node scripts/monitor.js\nRuns one live OpenStock monitoring cycle using .env configuration. The dashboard runs the same cycle every 30 seconds.\n');
    process.exit(0);
}

const dbPath = process.env.DATABASE_PATH || path.join(__dirname, '..', 'database/openstock.db');
const logger = createMonitorLogger(monitorPaths());

async function main() {
    if (!fs.existsSync(dbPath)) { logger.error(`Database not found: ${dbPath}`); return EXIT.CONFIG; }
    initDatabase({ silent: true });
    const db = new Database(dbPath);
    db.pragma('journal_mode = WAL'); db.pragma('foreign_keys = ON');
    try {
        return await runMonitorCycle({ db, makeClient: ({ timeoutMs }) => new AlpacaClient({ timeoutMs }), logger });
    } finally { db.close(); }
}

main().then(code => { process.exitCode = code; }).catch(errorValue => {
    logger.error(safeError(errorValue));
    process.exitCode = EXIT.CONFIG;
});
