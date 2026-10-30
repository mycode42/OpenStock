'use strict';

const { configuredWatchlist, makeQuote } = require('./alpaca-profile');

function createAlpacaQuoteFeed({ app, now = () => new Date(), refreshMs = Number(process.env.ALPACA_LIVE_PRICE_REFRESH_MS || 5000) } = {}) {
    const ttlMs = Math.min(Math.max(Number(refreshMs) || 5000, 1000), 30000);
    let cached = null;
    let inflight = null;

    async function refresh() {
        const stocks = configuredWatchlist(app.locals.db);
        const result = await app.locals.alpacaClient.getLatestQuotes(stocks.map(stock => stock.symbol));
        return {
            observed_at: now().toISOString(),
            refresh_ms: ttlMs,
            feed: result.feed,
            prices: stocks.map(stock => {
                const quote = makeQuote(result.quotes[stock.symbol]);
                return quote ? {
                    stock_id: stock.id,
                    symbol: stock.symbol,
                    currency: 'USD',
                    ...quote,
                    feed: result.feed,
                    // Demo quotes are simulated, so they are labelled as demo data rather than as Alpaca quotes.
                    source: result.feed === 'simulated' ? 'demo' : `alpaca_${result.feed}_quote`
                } : null;
            }).filter(Boolean)
        };
    }

    return {
        async get({ force = false } = {}) {
            const age = cached ? now().getTime() - Date.parse(cached.observed_at) : Infinity;
            if (!force && cached && age < ttlMs) return { ...cached, cached: true, refreshing: false };
            if (!inflight) inflight = refresh().then(value => { cached = value; return value; }).finally(() => { inflight = null; });
            if (cached && !force) return { ...cached, cached: true, refreshing: true };
            return { ...(await inflight), cached: false, refreshing: false };
        }
    };
}

module.exports = { createAlpacaQuoteFeed };
