function hasAlpacaCredentials(env = process.env) {
    return Boolean(String(env.ALPACA_MANUAL_API_KEY || '').trim() && String(env.ALPACA_MANUAL_API_SECRET || '').trim());
}

// The Algo Trader lives at ./algo-trader and is optional, so only the Alpaca setup is reported here.
function detectDemoSetup({ env = process.env } = {}) {
    const alpacaConfigured = hasAlpacaCredentials(env);
    const setup = [];
    if (!alpacaConfigured) setup.push('Add Alpaca API credentials to .env');
    return { demo_mode: !alpacaConfigured, setup, alpaca_configured: alpacaConfigured };
}

module.exports = {
    detectDemoSetup,
    hasAlpacaCredentials,
};
