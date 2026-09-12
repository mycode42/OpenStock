#!/usr/bin/env node
'use strict';

require('dotenv').config({ path: require('node:path').join(__dirname, '..', '.env'), quiet: true });
const fs = require('node:fs');
const path = require('node:path');
const Database = require('better-sqlite3');
const { initDatabase } = require('../database/init');
const { EXIT, runMonitor, safeError } = require('../lib/monitor');

if (process.argv.includes('--help')) {
    process.stdout.write('Usage: node scripts/monitor.js\nRuns one live OpenStock monitoring cycle using .env configuration.\n');
    process.exit(0);
}

const appDir = path.join(__dirname, '..');
const memoryDir = process.env.MONITOR_MEMORY_DIR || path.join(appDir, 'memory');
const dbPath = process.env.DATABASE_PATH || path.join(appDir, 'database/openstock.db');
const logPath = process.env.MONITOR_LOG || path.join(memoryDir, 'monitor.log');
const errorPath = process.env.MONITOR_ERROR_LOG || path.join(memoryDir, 'monitor-error.log');
fs.mkdirSync(memoryDir, { recursive: true });
function lineFor(message) { return `[${new Date().toISOString()}] ${message}\n`; }
function log(message) { const line = lineFor(message); fs.appendFileSync(logPath, line); process.stdout.write(line); }
function error(message) {
    const line = lineFor(`ERROR: ${message}`);
    fs.appendFileSync(errorPath, line);
    if (errorPath !== logPath) fs.appendFileSync(logPath, line);
    process.stdout.write(line);
}
function positiveSetting(name, fallback) {
    const value = Number(process.env[name] ?? fallback);
    if (!Number.isFinite(value) || value <= 0) throw new Error(`${name} must be a positive number`);
    return value;
}

async function main() {
    if (!fs.existsSync(dbPath)) { error(`Database not found: ${dbPath}`); return EXIT.CONFIG; }
    initDatabase({ silent: true });
    const db = new Database(dbPath);
    db.pragma('journal_mode = WAL'); db.pragma('foreign_keys = ON');
    const chatId = process.env.TELEGRAM_CHAT_ID || '';
    if (!chatId) error('No Telegram recipient configured; trigger notifications will fail safely');
    try {
        const timeoutMs = positiveSetting('MONITOR_TIMEOUT_MS', 10000);
        const fxTtlMs = positiveSetting('EXCHANGE_RATE_CACHE_SECONDS', 3600) * 1000;
        log('=== Monitoring cycle started ===');
        return await runMonitor({ db, log, error, config: {
            timeoutMs,
            yahooUrl: process.env.YAHOO_CHART_URL || 'https://query1.finance.yahoo.com/v8/finance/chart',
            lockPath: process.env.MONITOR_LOCK_PATH || path.join(memoryDir, 'monitor.lock'),
            fx: { cachePath: process.env.FX_CACHE_FILE || path.join(memoryDir, 'exchange_rate_cache.json'), ttlMs: fxTtlMs, url: process.env.FX_URL || 'https://api.exchangerate-api.com/v4/latest/USD' },
            telegram: { token: process.env.TELEGRAM_BOT_TOKEN, chatId }
        }});
    } catch (monitorError) { error(safeError(monitorError)); return EXIT.CONFIG; }
    finally { db.close(); }
}

main().then(code => { log(`Monitoring cycle complete (exit=${code})`); process.exitCode = code; }).catch(errorValue => { error(safeError(errorValue)); process.exitCode = EXIT.CONFIG; });
