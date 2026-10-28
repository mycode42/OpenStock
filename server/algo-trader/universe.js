const fs = require('node:fs/promises');
const path = require('node:path');
const { parse: parseDotenv } = require('dotenv');
const { finite, isoTimestamp, readCoordinatorState, safeObject } = require('./io');

const UNIVERSE_QUOTE_CACHE_MS = 10_000;
const universeQuoteCache = new Map();
const universeQuoteRequests = new Map();
const UNIVERSE_HISTORY_CACHE_MS = 60_000;
const UNIVERSE_HISTORY_RANGES = Object.freeze({
    '1d': { timeframe: '5Min', milliseconds: 24 * 60 * 60_000 },
    '1w': { timeframe: '30Min', milliseconds: 7 * 24 * 60 * 60_000 },
    '1mo': { timeframe: '1Hour', milliseconds: 31 * 24 * 60 * 60_000 },
    '1y': { timeframe: '1Day', milliseconds: 366 * 24 * 60 * 60_000 },
    '5y': { timeframe: '1Week', milliseconds: 5 * 366 * 24 * 60 * 60_000 },
});
const universeHistoryCache = new Map();
const universeHistoryRequests = new Map();
async function readAlgoAlpacaCredentials(root) {
    let fileEnvironment = {};
    try {
        fileEnvironment = parseDotenv(await fs.readFile(path.join(root, '.env'), 'utf8'));
    } catch (error) {
        if (error.code !== 'ENOENT') throw error;
    }
    return {
        key: process.env.ALPACA_ALGO_API_KEY || fileEnvironment.ALPACA_ALGO_API_KEY || '',
        secret: process.env.ALPACA_ALGO_API_SECRET || fileEnvironment.ALPACA_ALGO_API_SECRET || '',
    };
}

function normalizeAlpacaQuote(raw) {
    if (!raw || typeof raw !== 'object') return null;
    if (!raw.t) return null;
    const timestamp = new Date(raw.t);
    if (Number.isNaN(timestamp.getTime())) return null;
    const side = value => {
        const number = Number(value);
        return value !== null && value !== undefined && value !== '' && Number.isFinite(number) && number > 0 ? number : null;
    };
    const bid = side(raw.bp);
    const ask = side(raw.ap);
    if (bid === null && ask === null) return null;
    if (bid !== null && ask !== null && ask < bid) return null;
    return {
        bid,
        ask,
        midpoint: bid !== null && ask !== null ? (bid + ask) / 2 : null,
        timestamp: timestamp.toISOString(),
    };
}

async function fetchAlpacaUniverseQuotes(root, symbols) {
    const symbolsKey = symbols.join(',');
    if (!symbols.length) return { quotes: {}, status: 'empty', observed_at: new Date().toISOString() };
    const cached = universeQuoteCache.get(symbolsKey);
    if (cached && Date.now() - cached.cachedAt < UNIVERSE_QUOTE_CACHE_MS) return cached.value;
    if (universeQuoteRequests.has(symbolsKey)) return universeQuoteRequests.get(symbolsKey);

    const request = (async () => {
        try {
            const credentials = await readAlgoAlpacaCredentials(root);
            if (!credentials.key || !credentials.secret) throw new Error('credentials unavailable');
            const url = new URL('https://data.alpaca.markets/v2/stocks/quotes/latest');
            url.searchParams.set('symbols', symbolsKey);
            url.searchParams.set('feed', 'sip');
            const response = await fetch(url, {
                headers: {
                    Accept: 'application/json',
                    'APCA-API-KEY-ID': credentials.key,
                    'APCA-API-SECRET-KEY': credentials.secret,
                },
                signal: AbortSignal.timeout(12_000),
            });
            if (!response.ok) throw new Error(`Alpaca returned HTTP ${response.status}`);
            const payload = await response.json();
            if (!payload || !payload.quotes || typeof payload.quotes !== 'object') throw new Error('Alpaca returned an invalid quote response');
            const quotes = {};
            for (const symbol of symbols) {
                const quote = normalizeAlpacaQuote(payload.quotes[symbol]);
                if (quote) quotes[symbol] = quote;
            }
            const value = {
                quotes,
                status: Object.keys(quotes).length === symbols.length ? 'ok' : Object.keys(quotes).length ? 'partial' : 'unavailable',
                observed_at: new Date().toISOString(),
                error: Object.keys(quotes).length ? null : 'Alpaca returned no valid quotes for this universe.',
            };
            if (Object.keys(quotes).length) {
                universeQuoteCache.set(symbolsKey, { value, cachedAt: Date.now() });
            } else if (cached?.value) {
                return { ...cached.value, status: 'stale', error: value.error };
            }
            return value;
        } catch (error) {
            console.warn(`Algo universe Alpaca quotes unavailable: ${error.message}`);
            if (cached?.value) {
                return { ...cached.value, status: 'stale', error: 'Alpaca is temporarily unavailable; showing the latest received quotes.' };
            }
            return { quotes: {}, status: 'unavailable', observed_at: null, error: 'Alpaca quotes are temporarily unavailable.' };
        }
    })();
    universeQuoteRequests.set(symbolsKey, request);
    try { return await request; }
    finally { universeQuoteRequests.delete(symbolsKey); }
}

async function fetchAlpacaUniverseHistory(root, symbol, range) {
    const rangeConfig = UNIVERSE_HISTORY_RANGES[range];
    const cacheKey = `${symbol}:${range}`;
    const cached = universeHistoryCache.get(cacheKey);
    if (cached && Date.now() - cached.cachedAt < UNIVERSE_HISTORY_CACHE_MS) return cached.value;
    if (universeHistoryRequests.has(cacheKey)) return universeHistoryRequests.get(cacheKey);

    const request = (async () => {
        try {
            const credentials = await readAlgoAlpacaCredentials(root);
            if (!credentials.key || !credentials.secret) throw new Error('credentials unavailable');
            const end = new Date();
            const start = new Date(end.getTime() - rangeConfig.milliseconds);
            const url = new URL(`https://data.alpaca.markets/v2/stocks/${encodeURIComponent(symbol)}/bars`);
            url.searchParams.set('timeframe', rangeConfig.timeframe);
            url.searchParams.set('start', start.toISOString());
            url.searchParams.set('end', end.toISOString());
            url.searchParams.set('limit', '1000');
            url.searchParams.set('sort', 'asc');
            url.searchParams.set('feed', 'sip');
            url.searchParams.set('adjustment', 'split');
            const response = await fetch(url, {
                headers: {
                    Accept: 'application/json',
                    'APCA-API-KEY-ID': credentials.key,
                    'APCA-API-SECRET-KEY': credentials.secret,
                },
                signal: AbortSignal.timeout(15_000),
            });
            if (!response.ok) throw new Error(`Alpaca returned HTTP ${response.status}`);
            const payload = await response.json();
            if (!payload || !Array.isArray(payload.bars)) throw new Error('Alpaca returned an invalid bars response');
            const points = payload.bars.map(bar => ({ price: finite(bar?.c), recorded_at: isoTimestamp(bar?.t) }))
                .filter(point => point.price !== null && point.price > 0 && point.recorded_at);
            const value = {
                points,
                start_at: start.toISOString(),
                end_at: end.toISOString(),
                observed_at: new Date().toISOString(),
                stale: false,
            };
            if (points.length) universeHistoryCache.set(cacheKey, { value, cachedAt: Date.now() });
            else if (cached?.value) return { ...cached.value, stale: true };
            return value;
        } catch (error) {
            console.warn(`Algo universe Alpaca history unavailable for ${symbol}: ${error.message}`);
            if (cached?.value) return { ...cached.value, stale: true };
            return { points: [], start_at: null, end_at: null, observed_at: null, stale: false };
        }
    })();
    universeHistoryRequests.set(cacheKey, request);
    try { return await request; }
    finally { universeHistoryRequests.delete(cacheKey); }
}

async function buildUniversePayload(root) {
    const state = await readCoordinatorState(root);
    const portfolio = safeObject(state.strategy_portfolio);
    const universe = safeObject(portfolio.universe);
    const categories = safeObject(universe.categories);
    const sectors = safeObject(universe.sector_by_symbol);
    const symbols = [...new Set((Array.isArray(universe.tickers) ? universe.tickers : [])
        .map(value => String(value || '').trim().toUpperCase())
        .filter(value => /^[A-Z0-9.-]{1,16}$/.test(value)))];
    const quoteData = await fetchAlpacaUniverseQuotes(root, symbols);
    return {
        selection_status: universe.error ? 'error' : symbols.length ? 'ready' : 'empty',
        selection_date: String(universe.selection_date || '').slice(0, 10) || null,
        selector_as_of: String(universe.selector_as_of || '').slice(0, 10) || null,
        last_scan_at: isoTimestamp(portfolio.last_scan_at),
        market_state: String(state.market_state || 'UNKNOWN'),
        symbol_count: symbols.length,
        quote_status: quoteData.status,
        quote_feed: 'Alpaca SIP',
        quotes_observed_at: quoteData.observed_at,
        quote_error: quoteData.error || null,
        symbols: symbols.map(symbol => ({
            symbol,
            category: String(categories[symbol] || ''),
            sector: String(sectors[symbol] || ''),
            quote: quoteData.quotes[symbol] || null,
        })),
    };
}

module.exports = {
    UNIVERSE_QUOTE_CACHE_MS,
    universeQuoteCache,
    universeQuoteRequests,
    UNIVERSE_HISTORY_CACHE_MS,
    UNIVERSE_HISTORY_RANGES,
    universeHistoryCache,
    universeHistoryRequests,
    readAlgoAlpacaCredentials,
    normalizeAlpacaQuote,
    fetchAlpacaUniverseQuotes,
    fetchAlpacaUniverseHistory,
    buildUniversePayload,
};
