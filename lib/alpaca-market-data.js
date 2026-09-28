'use strict';

const { finitePositive, fetchJson } = require('./market-data');
const ALLOWED_FEEDS = new Set(['iex', 'sip', 'delayed_sip', 'boats', 'overnight', 'otc']);
function configuredValue(value) { return typeof value === 'string' && value.trim() ? value.trim() : null; }
function normalizedSymbol(symbol) {
    const value = String(symbol || '').trim().toUpperCase();
    if (!/^[A-Z0-9.\-/]{1,32}$/.test(value)) throw new Error('Invalid Alpaca symbol');
    return value;
}
function isoTimestamp(value) {
    const timestamp = new Date(value);
    return Number.isNaN(timestamp.getTime()) ? null : timestamp.toISOString();
}
function positiveNumber(value) {
    const number = Number(value);
    return Number.isFinite(number) && number > 0 ? number : null;
}
function nonNegativeNumber(value) {
    const number = Number(value);
    return Number.isFinite(number) && number >= 0 ? number : null;
}
function sanitizeBar(bar) {
    if (!bar || typeof bar !== 'object') return null;
    const timestamp = isoTimestamp(bar.t);
    const close = positiveNumber(bar.c);
    if (!timestamp || !close) return null;
    return { open_usd: positiveNumber(bar.o), high_usd: positiveNumber(bar.h), low_usd: positiveNumber(bar.l), close_usd: close, volume: nonNegativeNumber(bar.v), vwap_usd: positiveNumber(bar.vw), trade_count: nonNegativeNumber(bar.n), time: timestamp };
}
function sanitizeSnapshot(payload, { symbol, feed = 'iex' } = {}) {
    if (!payload || typeof payload !== 'object') throw new Error('Alpaca snapshot is unavailable');
    const quote = payload.latestQuote || {};
    const trade = payload.latestTrade || {};
    const bid = positiveNumber(quote.bp), ask = positiveNumber(quote.ap), tradePrice = positiveNumber(trade.p);
    if (!bid && !ask && !tradePrice) throw new Error('Alpaca snapshot has no usable market data');
    return {
        symbol: normalizedSymbol(symbol), feed: ALLOWED_FEEDS.has(String(feed).toLowerCase()) ? String(feed).toLowerCase() : 'iex', currency: 'USD',
        latest_quote: (bid || ask) ? { bid_usd: bid, bid_size: nonNegativeNumber(quote.bs), ask_usd: ask, ask_size: nonNegativeNumber(quote.as), time: isoTimestamp(quote.t), bid_exchange: typeof quote.bx === 'string' ? quote.bx.slice(0, 8) : null, ask_exchange: typeof quote.ax === 'string' ? quote.ax.slice(0, 8) : null } : null,
        latest_trade: tradePrice ? { price_usd: tradePrice, size: nonNegativeNumber(trade.s), time: isoTimestamp(trade.t), exchange: typeof trade.x === 'string' ? trade.x.slice(0, 8) : null } : null,
        minute_bar: sanitizeBar(payload.minuteBar), daily_bar: sanitizeBar(payload.dailyBar), previous_daily_bar: sanitizeBar(payload.prevDailyBar)
    };
}
function parseAlpacaLatestQuote(payload, { symbol, feed = 'iex', now = new Date() } = {}) {
    const quote = payload?.quote;
    if (!quote || typeof quote !== 'object') throw new Error('Alpaca response contains no quote');
    const bid = Number(quote.bp), ask = Number(quote.ap), validBid = Number.isFinite(bid) && bid > 0, validAsk = Number.isFinite(ask) && ask > 0;
    if (!validBid && !validAsk) throw new Error('Alpaca quote has no valid bid or ask');
    const timestamp = new Date(quote.t || now);
    if (Number.isNaN(timestamp.getTime())) throw new Error('Alpaca quote has an invalid timestamp');
    const priceUsd = finitePositive(validBid && validAsk ? (bid + ask) / 2 : (validBid ? bid : ask), 'Alpaca USD price');
    const normalizedFeed = ALLOWED_FEEDS.has(String(feed).toLowerCase()) ? String(feed).toLowerCase() : 'iex';
    return { symbol: String(symbol || '').toUpperCase(), priceUsd, bidUsd: validBid ? bid : null, askUsd: validAsk ? ask : null, quoteTime: timestamp.toISOString(), source: `alpaca_${normalizedFeed}_quote` };
}
class AlpacaMarketDataClient {
    constructor({ keyId = process.env.ALPACA_API_KEY_ID || process.env['APCA-API-KEY-ID'], secretKey = process.env.ALPACA_API_SECRET_KEY || process.env['APCA-API-SECRET-KEY'], baseUrl = process.env.ALPACA_DATA_BASE_URL || 'https://data.alpaca.markets', feed = process.env.ALPACA_DATA_FEED || 'iex', timeoutMs = Number(process.env.ALPACA_DATA_TIMEOUT_MS || 10000), fetchImpl = globalThis.fetch } = {}) {
        this.keyId = configuredValue(keyId); this.secretKey = configuredValue(secretKey); this.baseUrl = String(baseUrl || '').replace(/\/+$/, '');
        this.feed = ALLOWED_FEEDS.has(String(feed).toLowerCase()) ? String(feed).toLowerCase() : 'iex'; this.timeoutMs = Number.isFinite(timeoutMs) && timeoutMs > 0 ? timeoutMs : 10000; this.fetchImpl = fetchImpl;
    }
    configured() { return Boolean(this.keyId && this.secretKey && this.baseUrl); }
    async getLatestQuote(symbol) {
        const normalized = normalizedSymbol(symbol);
        if (!this.configured()) throw new Error('Alpaca market data is not configured');
        const endpoint = new URL(`/v2/stocks/${encodeURIComponent(normalized)}/quotes/latest`, `${this.baseUrl}/`); endpoint.searchParams.set('feed', this.feed);
        const payload = await fetchJson(endpoint.toString(), { fetchImpl: this.fetchImpl, timeoutMs: this.timeoutMs, headers: { Accept: 'application/json', 'APCA-API-KEY-ID': this.keyId, 'APCA-API-SECRET-KEY': this.secretKey } });
        return parseAlpacaLatestQuote(payload, { symbol: normalized, feed: this.feed });
    }
    async getSnapshot(symbol) {
        const normalized = normalizedSymbol(symbol);
        if (!this.configured()) throw new Error('Alpaca market data is not configured');
        const endpoint = new URL(`/v2/stocks/${encodeURIComponent(normalized)}/snapshot`, `${this.baseUrl}/`); endpoint.searchParams.set('feed', this.feed);
        const payload = await fetchJson(endpoint.toString(), { fetchImpl: this.fetchImpl, timeoutMs: this.timeoutMs, headers: { Accept: 'application/json', 'APCA-API-KEY-ID': this.keyId, 'APCA-API-SECRET-KEY': this.secretKey } });
        return sanitizeSnapshot(payload, { symbol: normalized, feed: this.feed });
    }
}
module.exports = { AlpacaMarketDataClient, parseAlpacaLatestQuote, sanitizeSnapshot, ALLOWED_FEEDS };
