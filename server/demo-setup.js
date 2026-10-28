const path = require('node:path');
const fs = require('node:fs');
const paths = require('../lib/paths');
const { TradeRepublicClient, BrokerError, configuredKey } = require('../lib/brokers/trade-republic');

function detectDemoSetup({ algoTraderRoot = paths.algoTraderRoot(), env = process.env, sdkClient = null } = {}) {
    const tradeRepublicKey = configuredKey(
        env,
        env.OPENSTOCK_CONFIG_FILE || (env === process.env ? paths.configFilePath() : ''),
    );
    const sessionFile = String(env.TRADE_REPUBLIC_SDK_SESSION_FILE || '').trim();
    const modulePath = paths.sdkModulePath(env);
    const sdkEnabled =
        String(env.TRADE_REPUBLIC_SDK_ENABLED ?? 'true').toLowerCase() === 'true' && sdkClient?.isEnabled?.() !== false;
    const sdkConfigured =
        sdkEnabled &&
        Boolean(sessionFile) &&
        fs.existsSync(modulePath) &&
        (fs.existsSync(sessionFile) || Boolean(env.TRADE_REPUBLIC_SDK_PHONE_NUMBER && env.TRADE_REPUBLIC_SDK_PIN));
    const tradeRepublicConfigured = Boolean(String(tradeRepublicKey || '').trim() || sdkConfigured);
    const algoTraderConfigured = fs.existsSync(path.join(algoTraderRoot, 'data', 'algo_state.sqlite3'));
    const setup = [];
    if (!tradeRepublicConfigured) setup.push('Add Trade Republic API key or SDK session details to .env');
    if (!algoTraderConfigured) setup.push("Algo Trader isn't connected; set ALGO_TRADER_ROOT in .env to connect it");
    return { demo_mode: setup.length > 0, setup };
}

module.exports = {
    detectDemoSetup,
};
