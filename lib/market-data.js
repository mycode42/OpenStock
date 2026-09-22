'use strict';

const fs = require('node:fs');
const fsp = require('node:fs/promises');
const path = require('node:path');
const crypto = require('node:crypto');

const VALID_YAHOO_SOURCES = new Set(['yahoo_pre_close', 'yahoo_regular_close', 'yahoo_post_close', 'yahoo_extended_close', 'yahoo_regular_market_price']);

function finitePositive(value, label) {
    const number = Number(value);
    if (!Number.isFinite(number) || number <= 0) throw new Error(`Invalid ${label}`);
    return number;
}

function safeError(error) {
    return String(error?.message || error || 'unknown error')
        .replace(/[\r\n\t]/g, ' ')
        .replace(/(bot)[^/\s]*/gi, '$1[redacted]')
        .slice(0, 300);
}

function parseYahooQuote(payload) {
    if (!payload || typeof payload !== 'object') throw new Error('Yahoo response is not an object');
    const result = payload.chart?.result?.[0];
    if (!result) throw new Error('Yahoo response contains no quote result');
    const meta = result.meta || {}, quote = result.indicators?.quote?.[0] || {};
    const closes = Array.isArray(quote.close) ? quote.close : [], timestamps = Array.isArray(result.timestamp) ? result.timestamp : [];
    let price, quoteTime = null;
    for (let index = closes.length - 1; index >= 0; index -= 1) {
        if (closes[index] !== null && Number.isFinite(Number(closes[index]))) { price = Number(closes[index]); quoteTime = Number.isFinite(Number(timestamps[index])) ? Number(timestamps[index]) : null; break; }
    }
    let session = 'extended';
    if (quoteTime !== null) for (const candidate of ['pre', 'regular', 'post']) { const period = meta.currentTradingPeriod?.[candidate] || {}; if (Number(period.start) <= quoteTime && quoteTime <= Number(period.end)) { session = candidate; break; } }
    let source = `yahoo_${session}_close`;
    if (price === undefined && Number.isFinite(Number(meta.regularMarketPrice))) { price = Number(meta.regularMarketPrice); source = 'yahoo_regular_market_price'; }
    finitePositive(price, 'Yahoo price');
    return { priceUsd: price, quoteTime, source: VALID_YAHOO_SOURCES.has(source) ? source : 'unknown', name: String(meta.shortName || meta.longName || '').slice(0, 200) };
}

function parseTradeRepublicMonitorQuote(quote, now = new Date()) {
    if (!quote || typeof quote !== 'object') throw new Error('Trade Republic quote unavailable');
    const currency = String(quote.currency || '').toUpperCase(), bidUnit = String(quote.bid_unit || currency).toUpperCase(), askUnit = String(quote.ask_unit || currency).toUpperCase();
    if (currency !== 'EUR' || bidUnit !== 'EUR' || askUnit !== 'EUR') throw new Error('Trade Republic quote is not EUR');
    const bid = Number(quote.bid_eur), ask = Number(quote.ask_eur), validBid = Number.isFinite(bid) && bid > 0, validAsk = Number.isFinite(ask) && ask > 0;
    if (!validBid && !validAsk) throw new Error('Trade Republic quote has no valid executable side');
    const quoteTime = quote.bid_time || quote.ask_time || quote.quote_time || now.toISOString(), parsedTime = new Date(quoteTime);
    return { priceEur: finitePositive(validBid && validAsk ? (bid + ask) / 2 : (validBid ? bid : ask), 'Trade Republic EUR price'), bidEur: validBid ? bid : null, askEur: validAsk ? ask : null, side: validBid && validAsk ? 'midpoint' : (validAsk ? 'ask' : 'bid'), quoteTime: Number.isNaN(parsedTime.getTime()) ? now.toISOString() : parsedTime.toISOString(), source: 'trade_republic_order_price' };
}

function readCache(cachePath, now = Date.now()) {
    try { const value = JSON.parse(fs.readFileSync(cachePath, 'utf8')), rate = finitePositive(value.rate, 'cached exchange rate'), timestamp = Number(value.timestamp) * 1000; if (!Number.isFinite(timestamp)) throw new Error('invalid cache timestamp'); return { rate, timestamp, fresh: now - timestamp >= 0 }; } catch { return null; }
}

async function writeCache(cachePath, rate, now = Date.now()) {
    await fsp.mkdir(path.dirname(cachePath), { recursive: true });
    const temporary = `${cachePath}.${process.pid}.${crypto.randomUUID()}.tmp`;
    await fsp.writeFile(temporary, JSON.stringify({ rate, timestamp: Math.floor(now / 1000) }) + '\n', { mode: 0o600 });
    await fsp.rename(temporary, cachePath);
}

async function fetchJson(url, { fetchImpl = globalThis.fetch, timeoutMs = 10000, headers = {}, method = 'GET', body } = {}) {
    if (typeof fetchImpl !== 'function') throw new Error('fetch is unavailable');
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), timeoutMs);
    try { const response = await fetchImpl(url, { method, headers, body, signal: controller.signal }); if (!response?.ok) throw new Error(`HTTP ${response?.status || 'failure'} for ${String(url).slice(0, 120)}`); return await response.json(); } finally { clearTimeout(timer); }
}

async function getExchangeRate({ cachePath, ttlMs, url, fetchImpl, now = Date.now(), log = () => {} }) {
    const cached = readCache(cachePath, now);
    if (cached && now - cached.timestamp < ttlMs) return { rate: cached.rate, source: 'cache' };
    try { const payload = await fetchJson(url, { fetchImpl, timeoutMs: 10000, headers: { Accept: 'application/json' } }), rate = finitePositive(payload?.rates?.EUR, 'exchange rate'); await writeCache(cachePath, rate, now); return { rate, source: 'api' }; }
    catch (error) { if (cached) { log(`FX API unavailable; using stale cached rate (${safeError(error)})`); return { rate: cached.rate, source: 'stale-cache' }; } throw new Error(`exchange rate unavailable: ${safeError(error)}`); }
}

async function fetchYahooFallbackQuote({ symbol, yahooUrl, fx, fetchImpl = globalThis.fetch, timeoutMs = 10000, now = Date.now(), log = () => {}, exchangeRateResult = null }) {
    const exchangeRate = exchangeRateResult || await getExchangeRate({ ...fx, fetchImpl, now, log });
    const url = `${yahooUrl}/${encodeURIComponent(symbol)}?range=1d&interval=1m&includePrePost=true&corsDomain=finance.yahoo.com&_=${now}`;
    const quote = parseYahooQuote(await fetchJson(url, { fetchImpl, timeoutMs, headers: { Accept: 'application/json', 'User-Agent': 'Mozilla/5.0 OpenStock/1.0', 'Cache-Control': 'no-cache', Pragma: 'no-cache' } }));
    return { ...quote, priceEur: quote.priceUsd * exchangeRate.rate, exchangeRate: exchangeRate.rate, exchangeRateSource: exchangeRate.source, fallback: true };
}

module.exports = { finitePositive, safeError, parseYahooQuote, parseTradeRepublicMonitorQuote, readCache, writeCache, fetchJson, getExchangeRate, fetchYahooFallbackQuote };
