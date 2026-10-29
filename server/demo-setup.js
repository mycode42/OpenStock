const path = require('node:path');
const fs = require('node:fs');
const paths = require('../lib/paths');

function hasAlpacaCredentials(env = process.env) {
    return Boolean(String(env.ALPACA_MANUAL_API_KEY || '').trim() && String(env.ALPACA_MANUAL_API_SECRET || '').trim());
}

function detectDemoSetup({ algoTraderRoot = paths.algoTraderRoot(), env = process.env } = {}) {
    const alpacaConfigured = hasAlpacaCredentials(env);
    const algoTraderConfigured = fs.existsSync(path.join(algoTraderRoot, 'data', 'algo_state.sqlite3'));
    const setup = [];
    if (!alpacaConfigured) setup.push('Add Alpaca API credentials to .env');
    if (!algoTraderConfigured) setup.push("Algo Trader isn't connected; set ALGO_TRADER_ROOT in .env to connect it");
    return { demo_mode: !alpacaConfigured, setup, alpaca_configured: alpacaConfigured };
}

module.exports = {
    detectDemoSetup,
    hasAlpacaCredentials,
};
