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
    const strategies = Array.isArray(value.strategies) ? value.strategies.slice(0, 10).map(strategy => ({
        id: /^[a-z][a-z0-9_-]{0,31}$/.test(String(strategy?.id || '')) ? strategy.id : null,
        symbol: /^[A-Z.]{1,12}$/.test(String(strategy?.symbol || '')) ? strategy.symbol : null,
        mode: ['percent_band', 'dip_rebuy'].includes(strategy?.mode) ? strategy.mode : null,
        buyDropPercent: safeNumber(strategy?.buyDropPercent), rebuyDropPercent: safeNumber(strategy?.rebuyDropPercent), sellRisePercent: safeNumber(strategy?.sellRisePercent), maxOrderUsd: safeNumber(strategy?.maxOrderUsd), budgetUsd: safeNumber(strategy?.budgetUsd), committedUsd: safeNumber(strategy?.committedUsd), remainingBudgetUsd: safeNumber(strategy?.remainingBudgetUsd), highAskUsd: safeNumber(strategy?.highAskUsd), managedLots: safeNumber(strategy?.managedLots), enabled: strategy?.enabled === true, pending: strategy?.pending === true
    })).filter(strategy => strategy.id && strategy.symbol && strategy.mode) : [];
    return {
        environment: value.environment === 'paper' ? 'paper' : 'unavailable', executionEnabled: value.executionEnabled === true,
        environmentGate: value.environmentGate === true, acknowledgementGate: value.acknowledgementGate === true,
        hardMaxOrderUsd: safeNumber(value.hardMaxOrderUsd), hardMaxBudgetUsd: safeNumber(value.hardMaxBudgetUsd), strategies,
        account: value.account && typeof value.account === 'object' ? { status: String(value.account.status || ''), tradingBlocked: value.account.tradingBlocked === true, accountBlocked: value.account.accountBlocked === true, buyingPowerUsd: safeNumber(value.account.buyingPowerUsd), cashUsd: safeNumber(value.account.cashUsd), patternDayTrader: value.account.patternDayTrader === true } : null,
        clock: value.clock && typeof value.clock === 'object' ? { isOpen: value.clock.isOpen === true, timestamp: typeof value.clock.timestamp === 'string' ? value.clock.timestamp : null, nextOpen: typeof value.clock.nextOpen === 'string' ? value.clock.nextOpen : null, nextClose: typeof value.clock.nextClose === 'string' ? value.clock.nextClose : null } : null,
        loop: value.loop && typeof value.loop === 'object' ? { running: value.loop.running === true, inFlight: value.loop.inFlight === true, lastTickAt: typeof value.loop.lastTickAt === 'string' ? value.loop.lastTickAt : null, lastResult: typeof value.loop.lastResult === 'string' ? value.loop.lastResult.slice(0, 160) : null, pendingStrategyIds: Array.isArray(value.loop.pendingStrategyIds) ? value.loop.pendingStrategyIds.filter(id => /^[a-z][a-z0-9_-]{0,31}$/.test(String(id))).slice(0, 10) : [] } : null
    };
}

function validateConfig(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Trader configuration must be an object');
    const strategies = Array.isArray(value.strategies) ? value.strategies : [];
    if (!strategies.length || strategies.length > 10) throw new Error('Trader strategies must contain 1 to 10 entries');
    const number = (input, label, maximum) => { const parsed = Number(input); if (!Number.isFinite(parsed) || parsed <= 0 || parsed > maximum) throw new Error(`${label} is invalid`); return parsed; };
    const ids = new Set(); const symbols = new Set();
    return { strategies: strategies.map((entry, index) => {
        if (!entry || typeof entry !== 'object' || Array.isArray(entry)) throw new Error(`strategy ${index + 1} is invalid`);
        const id = String(entry.id || '').trim().toLowerCase(); const symbol = String(entry.symbol || '').trim().toUpperCase();
        if (!/^[a-z][a-z0-9_-]{0,31}$/.test(id) || ids.has(id)) throw new Error('Strategy IDs must be unique and valid'); ids.add(id);
        if (!/^[A-Z.]{1,12}$/.test(symbol) || symbols.has(symbol)) throw new Error('Strategy tickers must be unique US tickers'); symbols.add(symbol);
        if (!['percent_band', 'dip_rebuy'].includes(entry.mode)) throw new Error('Strategy mode is invalid');
        if (typeof entry.enabled !== 'boolean') throw new Error('enabled must be boolean');
        const maxOrderUsd = number(entry.maxOrderUsd, 'maxOrderUsd', 500); const budgetUsd = number(entry.budgetUsd, 'budgetUsd', 5000); if (budgetUsd < maxOrderUsd) throw new Error('budgetUsd must cover one maximum order');
        return { id, symbol, mode: entry.mode, buyDropPercent: number(entry.buyDropPercent, 'buyDropPercent', 20), rebuyDropPercent: number(entry.rebuyDropPercent, 'rebuyDropPercent', 20), sellRisePercent: number(entry.sellRisePercent, 'sellRisePercent', 20), maxOrderUsd, budgetUsd, enabled: entry.enabled };
    }) };
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
