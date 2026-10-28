#!/usr/bin/env node
'use strict';

// Starts OpenStock in demo mode: simulated quotes, no broker login, no Algo Trader data.
// Credential and config variables are overridden so a local .env is never read.
const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');

const appDir = path.join(__dirname, '..');
const demoDir = path.join(appDir, 'data', 'demo');
fs.mkdirSync(demoDir, { recursive: true });

const env = {
    ...process.env,
    // Deliberately absent: keeps the config loader from reading a real .env.
    OPENSTOCK_CONFIG_FILE: path.join(demoDir, 'no-config.env'),
    DATABASE_PATH: path.join(demoDir, 'openstock.db'),
    ALGO_TRADER_ROOT: path.join(demoDir, 'no-algo-trader'),
    TRADE_REPUBLIC_API_KEY: '',
    TR_UAPI_KEY: '',
    TRADE_REPUBLIC_SDK_ENABLED: 'false',
    TRADE_REPUBLIC_SDK_SESSION_FILE: '',
    TRADE_REPUBLIC_SDK_PHONE_NUMBER: '',
    TRADE_REPUBLIC_SDK_PIN: '',
};

const port = env.PORT || 3000;
process.stdout.write(`OpenStock demo mode: simulated data on http://127.0.0.1:${port}\n`);

const child = spawn(process.execPath, [path.join(appDir, 'server.js')], { stdio: 'inherit', env });
for (const signal of ['SIGINT', 'SIGTERM']) {
    process.on(signal, () => child.kill(signal));
}
child.on('exit', (code, signal) => process.exit(signal ? 1 : code ?? 0));
