'use strict';

// Default locations are resolved from this checkout, so OpenStock runs from any clone directory.
// Relative values in environment variables also resolve against the checkout root, not the shell's cwd.
const path = require('node:path');

const REPO_ROOT = path.resolve(__dirname, '..');

function fromRepo(value, fallback) {
    return path.resolve(REPO_ROOT, String(value ?? '').trim() || fallback);
}

module.exports = {
    REPO_ROOT,
    configFilePath: (env = process.env) => fromRepo(env.OPENSTOCK_CONFIG_FILE, '.env'),
    databasePath: (env = process.env) => fromRepo(env.DATABASE_PATH, 'database/openstock.db'),
    // Sibling installs: a clone next to ../algo-trader and ../handelsrepublik works without configuration.
    algoTraderRoot: (env = process.env) => fromRepo(env.ALGO_TRADER_ROOT, '../algo-trader'),
    sdkModulePath: (env = process.env) => fromRepo(env.TRADE_REPUBLIC_SDK_MODULE_PATH, '../handelsrepublik/dist/index.js'),
};
