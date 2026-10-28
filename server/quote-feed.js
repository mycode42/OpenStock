const { QuoteService } = require('../lib/quote-service');

function quoteServiceFor(app, now) {
    return new QuoteService({
        db: app.locals.db,
        brokerClient: app.locals.brokerClient,
        sdkClient: app.locals.sdkClient,
        now,
        log: (message) => console.warn(message),
        config: {
            timeoutMs: Number(process.env.PRICE_FETCH_TIMEOUT_MS || 10000),
            concurrency: Number(process.env.QUOTE_FETCH_CONCURRENCY || process.env.MONITOR_CONCURRENCY || 3),
        },
    });
}

function createLiveQuoteFeed({
    app,
    now = () => new Date(),
    refreshMs = Number(process.env.LIVE_PRICE_REFRESH_MS || 1000),
} = {}) {
    const ttlMs = Math.min(Math.max(Number(refreshMs) || 1000, 1000), 10000);
    let cached = null,
        inflight = null;
    async function refresh({ forceSdkAuthRetry = false } = {}) {
        const stocks = app.locals.db.getActiveStocks();
        const timeoutMs = Math.max(1000, Number(process.env.LIVE_PRICE_FETCH_DEADLINE_MS || 30000));
        const rows = await quoteServiceFor(app, now).fetchMany(
            stocks.map((stock) => ({ ...stock, mapping: app.locals.db.getBrokerMapping(stock.id) })),
            { deadlineAt: Date.now() + timeoutMs, forceSdkAuthRetry },
        );
        return {
            observed_at: now().toISOString(),
            refresh_ms: ttlMs,
            prices: rows
                .filter((row) => row.quote)
                .map(({ stock, quote }) => ({
                    stock_id: stock.id,
                    price_eur: quote.priceEur,
                    bid_eur: quote.bidEur ?? null,
                    ask_eur: quote.askEur ?? null,
                    quote_time: quote.quoteTime || null,
                    observed_at: quote.observedAt || null,
                    source: quote.source,
                    fallback: quote.source === 'trade_republic_order_price',
                })),
        };
    }
    let forceInflight = null;
    return {
        async get({ retrySdkAuth = false } = {}) {
            if (retrySdkAuth) {
                if (!forceInflight)
                    forceInflight = refresh({ forceSdkAuthRetry: true })
                        .then((value) => {
                            cached = value;
                            return value;
                        })
                        .finally(() => {
                            forceInflight = null;
                        });
                return { ...(await forceInflight), cached: false, refreshing: false };
            }
            const age = cached ? now().getTime() - Date.parse(cached.observed_at) : Infinity;
            if (cached && age < ttlMs) return { ...cached, cached: true, refreshing: false };
            if (!inflight)
                inflight = refresh()
                    .then((value) => {
                        cached = value;
                        return value;
                    })
                    .finally(() => {
                        inflight = null;
                    });
            if (cached) return { ...cached, cached: true, refreshing: true };
            return { ...(await inflight), cached: false, refreshing: false };
        },
    };
}

module.exports = {
    quoteServiceFor,
    createLiveQuoteFeed,
};
