const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');

const RUNNER_ORIGIN = 'http://127.0.0.1:4317';
const TOKEN_MINIMUM_LENGTH = 32;

function unavailable() { const error = new Error('Trader runner integration is not configured'); error.code = 'RUNNER_UNAVAILABLE'; throw error; }
function validToken(token) { return typeof token === 'string' && token.length >= TOKEN_MINIMUM_LENGTH; }
function runnerToken(environment = process.env) {
    if (validToken(environment.TRADER_RUNNER_API_TOKEN)) return environment.TRADER_RUNNER_API_TOKEN;
    try { return fs.readFileSync(path.join(environment.CREDENTIALS_DIRECTORY || '', 'trader-runner-api-token'), 'utf8').trim(); }
    catch { return ''; }
}
function safeNumber(value) { const number = Number(value); return Number.isFinite(number) ? number : null; }

function sanitizeStatus(value) {
    if (!value || typeof value !== 'object') throw new Error('Trader runner returned an invalid status');
    const strategy = value.strategy || {};
    return {
        environment: value.environment === 'paper' ? 'paper' : 'unavailable', executionEnabled: value.executionEnabled === true,
        environmentGate: value.environmentGate === true, acknowledgementGate: value.acknowledgementGate === true,
        hardMaxOrderUsd: safeNumber(value.hardMaxOrderUsd),
        strategy: { symbols: Array.isArray(strategy.symbols) ? strategy.symbols.filter(symbol => /^[A-Z.]{1,12}$/.test(String(symbol))).slice(0, 5) : [], strategy: strategy.strategy === 'percent_band' ? 'percent_band' : null, buyDropPercent: safeNumber(strategy.buyDropPercent), sellRisePercent: safeNumber(strategy.sellRisePercent), maxOrderUsd: safeNumber(strategy.maxOrderUsd), enabled: strategy.enabled === true },
        account: value.account && typeof value.account === 'object' ? { status: String(value.account.status || ''), tradingBlocked: value.account.tradingBlocked === true, accountBlocked: value.account.accountBlocked === true, buyingPowerUsd: safeNumber(value.account.buyingPowerUsd), cashUsd: safeNumber(value.account.cashUsd), patternDayTrader: value.account.patternDayTrader === true } : null,
        clock: value.clock && typeof value.clock === 'object' ? { isOpen: value.clock.isOpen === true, timestamp: typeof value.clock.timestamp === 'string' ? value.clock.timestamp : null, nextOpen: typeof value.clock.nextOpen === 'string' ? value.clock.nextOpen : null, nextClose: typeof value.clock.nextClose === 'string' ? value.clock.nextClose : null } : null
    };
}

function validateConfig(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Trader configuration must be an object');
    const symbols = Array.isArray(value.symbols) ? value.symbols.map(symbol => String(symbol || '').trim().toUpperCase()) : [];
    if (!symbols.length || symbols.length > 5 || new Set(symbols).size !== symbols.length || symbols.some(symbol => !/^[A-Z.]{1,12}$/.test(symbol))) throw new Error('Trader symbols must contain 1 to 5 unique US tickers');
    const number = (input, label, maximum) => { const parsed = Number(input); if (!Number.isFinite(parsed) || parsed <= 0 || parsed > maximum) throw new Error(`${label} is invalid`); return parsed; };
    if (value.strategy !== 'percent_band') throw new Error('Only percent_band is supported');
    if (typeof value.enabled !== 'boolean') throw new Error('enabled must be boolean');
    return { symbols, strategy: 'percent_band', buyDropPercent: number(value.buyDropPercent, 'buyDropPercent', 20), sellRisePercent: number(value.sellRisePercent, 'sellRisePercent', 20), maxOrderUsd: number(value.maxOrderUsd, 'maxOrderUsd', 500), enabled: value.enabled };
}

class TraderRunnerClient {
    constructor({ token = runnerToken(), request = null } = {}) { this.token = token; this.request = request || this.requestJson.bind(this); }
    configured() { return validToken(this.token); }
    requestJson(path, { method = 'GET', body } = {}) {
        return new Promise((resolve, reject) => {
            const payload = body ? JSON.stringify(body) : null;
            const request = http.request(`${RUNNER_ORIGIN}${path}`, { method, timeout: 5000, headers: { authorization: `Bearer ${this.token}`, accept: 'application/json', ...(payload ? { 'content-type': 'application/json', 'content-length': Buffer.byteLength(payload) } : {}) } }, response => {
                let output = ''; response.setEncoding('utf8'); response.on('data', chunk => { output += chunk; if (output.length > 16384) response.destroy(); });
                response.on('end', () => { let parsed; try { parsed = JSON.parse(output); } catch { return reject(new Error('Trader runner returned an invalid response')); } if (response.statusCode < 200 || response.statusCode >= 300) return reject(new Error(parsed?.error || 'Trader runner rejected the request')); resolve(parsed); });
            });
            request.on('timeout', () => request.destroy(new Error('Trader runner timed out'))); request.on('error', () => reject(new Error('Trader runner is unavailable'))); if (payload) request.write(payload); request.end();
        });
    }
    async status() { if (!this.configured()) unavailable(); return sanitizeStatus(await this.request('/v1/trading/status')); }
    async configure(input) { if (!this.configured()) unavailable(); return sanitizeStatus(await this.request('/v1/trading/config', { method: 'PUT', body: validateConfig(input) })); }
    async testBuy(symbol) { if (!this.configured()) unavailable(); const result = await this.request('/v1/trading/test-buy', { method: 'POST', body: { symbol: String(symbol || '').trim().toUpperCase(), confirmation: 'PLACE PAPER TEST BUY' } }); return { order: result?.order && typeof result.order === 'object' ? { id: typeof result.order.id === 'string' ? result.order.id : null, symbol: typeof result.order.symbol === 'string' ? result.order.symbol : null, side: result.order.side === 'buy' ? 'buy' : null, qty: safeNumber(result.order.qty), status: typeof result.order.status === 'string' ? result.order.status : null, submittedAt: typeof result.order.submittedAt === 'string' ? result.order.submittedAt : null } : null, executionEnabled: result?.executionEnabled === true }; }
}

module.exports = { TraderRunnerClient, validateConfig, sanitizeStatus, runnerToken };
