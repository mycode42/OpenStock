'use strict';

class AlpacaError extends Error {
    constructor(message, status = 502) {
        super(message);
        this.name = 'AlpacaError';
        this.status = status;
    }
}

class AlpacaClient {
    constructor({ env = process.env, fetchImpl = globalThis.fetch, timeoutMs = 10000 } = {}) {
        this.key = String(env.ALPACA_LIVE_API_KEY || '').trim();
        this.secret = String(env.ALPACA_LIVE_API_SECRET || '').trim();
        this.tradingBaseUrl = String(env.ALPACA_LIVE_BASE_URL || 'https://api.alpaca.markets').replace(/\/$/, '');
        this.dataBaseUrl = String(env.ALPACA_DATA_BASE_URL || 'https://data.alpaca.markets').replace(/\/$/, '');
        this.preferredFeed = String(env.ALPACA_DATA_FEED || 'sip').trim().toLowerCase() === 'iex' ? 'iex' : 'sip';
        this.fetchImpl = fetchImpl;
        this.timeoutMs = Math.max(1000, Number(timeoutMs) || 10000);
    }

    isConfigured() {
        return Boolean(this.key && this.secret);
    }

    headers() {
        return {
            'APCA-API-KEY-ID': this.key,
            'APCA-API-SECRET-KEY': this.secret,
            Accept: 'application/json'
        };
    }

    async request(baseUrl, path, { feedFallback = false } = {}) {
        if (!this.isConfigured()) throw new AlpacaError('Alpaca API credentials are not configured', 503);
        const feeds = feedFallback ? [this.preferredFeed, this.preferredFeed === 'sip' ? 'iex' : 'sip'] : [null];
        let lastResponse;
        for (let index = 0; index < feeds.length; index += 1) {
            const feed = feeds[index];
            const url = new URL(path, `${baseUrl}/`);
            if (feed) url.searchParams.set('feed', feed);
            let response;
            try {
                response = await this.fetchImpl(url, {
                    method: 'GET',
                    headers: this.headers(),
                    signal: AbortSignal.timeout(this.timeoutMs)
                });
            } catch {
                throw new AlpacaError('Alpaca API request failed before a response', 502);
            }
            lastResponse = response;
            if (response?.ok) {
                let payload;
                try { payload = await response.json(); }
                catch { throw new AlpacaError('Alpaca API returned invalid JSON', 502); }
                return { payload, feed };
            }
            if (!(feedFallback && response?.status === 403 && index === 0)) {
                throw new AlpacaError(`Alpaca API request failed (${response?.status || 'unknown status'})`, response?.status || 502);
            }
        }
        throw new AlpacaError(`Alpaca API request failed (${lastResponse?.status || 'unknown status'})`, lastResponse?.status || 502);
    }

    async getAccount() {
        return (await this.request(this.tradingBaseUrl, '/v2/account')).payload;
    }

    async getPositions() {
        return (await this.request(this.tradingBaseUrl, '/v2/positions')).payload;
    }

    async getFillActivities({ pageSize = 100 } = {}) {
        const path = `/v2/account/activities/FILL?direction=desc&page_size=${encodeURIComponent(pageSize)}`;
        return (await this.request(this.tradingBaseUrl, path)).payload;
    }

    async getLatestQuotes(symbols) {
        const clean = [...new Set((symbols || []).map(value => String(value).trim().toUpperCase()).filter(Boolean))];
        if (!clean.length) return { feed: this.preferredFeed, quotes: {} };
        const path = `/v2/stocks/quotes/latest?symbols=${encodeURIComponent(clean.join(','))}`;
        const result = await this.request(this.dataBaseUrl, path, { feedFallback: true });
        return { feed: result.feed, quotes: result.payload?.quotes || {} };
    }

    async getBars(symbol, { timeframe, start, end, limit = 1000 } = {}) {
        const params = new URLSearchParams({
            symbols: String(symbol).toUpperCase(),
            timeframe: String(timeframe || '1Day'),
            start: new Date(start).toISOString(),
            end: new Date(end).toISOString(),
            limit: String(Math.min(10000, Math.max(1, Number(limit) || 1000))),
            adjustment: 'split'
        });
        const result = await this.request(this.dataBaseUrl, `/v2/stocks/bars?${params}`, { feedFallback: true });
        return { feed: result.feed, bars: result.payload?.bars?.[String(symbol).toUpperCase()] || [] };
    }

    async getAsset(symbol) {
        return (await this.request(this.tradingBaseUrl, `/v2/assets/${encodeURIComponent(String(symbol).toUpperCase())}`)).payload;
    }
}

function makeQuote(quote) {
    if (!quote) return null;
    const bid = Number(quote.bp), ask = Number(quote.ap);
    const bidUsd = Number.isFinite(bid) && bid > 0 ? bid : null;
    const askUsd = Number.isFinite(ask) && ask > 0 ? ask : null;
    const priceUsd = bidUsd !== null && askUsd !== null ? (bidUsd + askUsd) / 2 : askUsd ?? bidUsd;
    return {
        price_usd: priceUsd,
        bid_usd: bidUsd,
        ask_usd: askUsd,
        quote_time: quote.t || null
    };
}

module.exports = { AlpacaClient, AlpacaError, makeQuote };
