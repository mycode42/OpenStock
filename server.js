require('dotenv').config({ path: process.env.OPENSTOCK_CONFIG_FILE || '/opt/openstock/.env', quiet: true });
const express = require('express');
const path = require('path');
const OpenStockDB = require('./database/db');
const { initDatabase } = require('./database/init');
const { calculateTrade, calculateDashboard, snapshotBucketAt } = require('./lib/analytics');
const { HistoryBackfillQueue } = require('./lib/history-backfill');
const { TradeRepublicClient, BrokerError } = require('./lib/brokers/trade-republic');
const { resolveVerifiedInstrument, resolveCryptoCandidates, resolveVerifiedCryptoInstrument } = require('./lib/instrument-resolution');
const { QuoteService } = require('./lib/quote-service');
const { HandelsrepublikQuoteClient } = require('./lib/brokers/handelsrepublik');
const APP_VERSION = require('./package.json').version;

// Convert SQLite or already-ISO UTC timestamps to one valid ISO 8601 value.
function toISODateTime(value) {
    if (!value) return null;
    const normalized = String(value).trim().replace(' ', 'T');
    const zoned = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(normalized) ? normalized : `${normalized}Z`;
    const parsed = new Date(zoned);
    return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString();
}

const BERLIN_DATE_TIME = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Europe/Berlin', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'
});

function berlinDateTimeParts(value) {
    return Object.fromEntries(BERLIN_DATE_TIME.formatToParts(value).filter(part => part.type !== 'literal').map(part => [part.type, Number(part.value)]));
}

function berlinTradingDayStart(now = new Date()) {
    const local = berlinDateTimeParts(now);
    let dateAtMidnight = Date.UTC(local.year, local.month - 1, local.day);
    if (local.hour * 60 + local.minute < 7 * 60 + 30) dateAtMidnight -= 86400000;
    const tradingDate = new Date(dateAtMidnight);
    const desired = {
        year: tradingDate.getUTCFullYear(), month: tradingDate.getUTCMonth() + 1, day: tradingDate.getUTCDate(),
        hour: 7, minute: 30, second: 0
    };
    let timestamp = Date.UTC(desired.year, desired.month - 1, desired.day, desired.hour, desired.minute, desired.second);
    for (let pass = 0; pass < 2; pass += 1) {
        const actual = berlinDateTimeParts(new Date(timestamp));
        timestamp += Date.UTC(desired.year, desired.month - 1, desired.day, desired.hour, desired.minute, desired.second)
            - Date.UTC(actual.year, actual.month - 1, actual.day, actual.hour, actual.minute, actual.second);
    }
    return new Date(timestamp);
}

function berlinTradingDayEnd(now = new Date()) {
    // The 1-day chart uses a fixed Berlin wall-clock session: 07:30–22:00.
    // DST changes occur outside this interval, so the endpoint is exactly
    // fourteen and a half hours after the matching session start.
    return new Date(berlinTradingDayStart(now).getTime() + (14 * 60 + 30) * 60000);
}

function evenlySample(points, maximum) {
    if (!Number.isInteger(maximum) || maximum < 2 || points.length <= maximum) return points;
    const sampled = [points[0]];
    const step = (points.length - 1) / (maximum - 1);
    for (let index = 1; index < maximum - 1; index += 1) sampled.push(points[Math.round(index * step)]);
    sampled.push(points[points.length - 1]);
    return sampled;
}

function serializeStock(stock) {
    if (!stock) return stock;
    const { owned: _legacyOwned, ...publicStock } = stock;
    return {
        ...publicStock,
        last_price_update: toISODateTime(stock.last_price_update),
        created_at: toISODateTime(stock.created_at),
        updated_at: toISODateTime(stock.updated_at)
    };
}

function serializeBrokerStock(stock, db) {
    if (!stock) return stock;
    const mapping = db.getBrokerMapping(stock.id);
    const quote = mapping ? db.getLatestBrokerQuote(mapping.isin, mapping.exchange) : null;
    const observation = db.getLatestPriceObservation(stock.id);
    const isTradeRepublicPrice = source => String(source || '').startsWith('trade_republic');
    const brokerPriceIsCurrent = Boolean(quote && isTradeRepublicPrice(observation?.source));
    const safeQuote = brokerPriceIsCurrent ? { bid_eur: quote.bid_eur, ask_eur: quote.ask_eur, spread_eur: quote.bid_eur !== null && quote.ask_eur !== null ? quote.ask_eur - quote.bid_eur : null, quote_time: quote.quote_time, observed_at: quote.observed_at, source: quote.source } : null;
    const fallback = observation?.source === 'trade_republic_order_price';
    const marketLabel = !observation ? 'No quote' : observation.source === 'trade_republic_sdk_order_price' ? 'Handelsrepublik SDK' : observation.source === 'trade_republic_order_price' ? 'Trade Republic UAPI sidecar' : 'Trade Republic history';
    return { ...serializeStock(stock), asset_type: stock.asset_type || 'stock', broker_mapping: mapping ? { broker: mapping.broker, ticker: mapping.ticker, isin: mapping.isin, exchange: mapping.exchange, currency: mapping.currency, asset_type: stock.asset_type || 'stock', verified_at: mapping.verified_at, source: mapping.source } : null, broker_quote: safeQuote,
        market_data: { source: observation?.source || null, label: marketLabel, fallback, quote_time: toISODateTime(observation?.quote_time) },
        position: db.getPositionSummary(stock.id), alert_levels: db.getAlertLevels(stock.id).map(serializeAlertLevel) };
}

function quoteServiceFor(app, now) {
    return new QuoteService({ db: app.locals.db, brokerClient: app.locals.brokerClient, sdkClient: app.locals.sdkClient, now, log: message => console.warn(message), config: { timeoutMs: Number(process.env.PRICE_FETCH_TIMEOUT_MS || 10000), concurrency: Number(process.env.QUOTE_FETCH_CONCURRENCY || process.env.MONITOR_CONCURRENCY || 3) } });
}

function createLiveQuoteFeed({ app, now = () => new Date(), refreshMs = Number(process.env.LIVE_PRICE_REFRESH_MS || 1000) } = {}) {
    const ttlMs = Math.min(Math.max(Number(refreshMs) || 1000, 1000), 10000);
    let cached = null, inflight = null;
    async function refresh({ forceSdkAuthRetry = false } = {}) {
        const stocks = app.locals.db.getActiveStocks();
        const timeoutMs = Math.max(1000, Number(process.env.LIVE_PRICE_FETCH_DEADLINE_MS || 30000));
        const rows = await quoteServiceFor(app, now).fetchMany(stocks.map(stock => ({ ...stock, mapping: app.locals.db.getBrokerMapping(stock.id) })), { deadlineAt: Date.now() + timeoutMs, forceSdkAuthRetry });
        return { observed_at: now().toISOString(), refresh_ms: ttlMs, prices: rows.filter(row => row.quote).map(({ stock, quote }) => ({ stock_id: stock.id, price_eur: quote.priceEur, bid_eur: quote.bidEur ?? null, ask_eur: quote.askEur ?? null, quote_time: quote.quoteTime || null, observed_at: quote.observedAt || null, source: quote.source, fallback: quote.source === 'trade_republic_order_price' })) };
    }
    let forceInflight = null;
    return { async get({ retrySdkAuth = false } = {}) {
        if (retrySdkAuth) {
            if (!forceInflight) forceInflight = refresh({ forceSdkAuthRetry: true }).then(value => { cached = value; return value; }).finally(() => { forceInflight = null; });
            return { ...(await forceInflight), cached: false, refreshing: false };
        }
        const age = cached ? now().getTime() - Date.parse(cached.observed_at) : Infinity;
        if (cached && age < ttlMs) return { ...cached, cached: true, refreshing: false };
        if (!inflight) inflight = refresh().then(value => { cached = value; return value; }).finally(() => { inflight = null; });
        if (cached) return { ...cached, cached: true, refreshing: true };
        return { ...(await inflight), cached: false, refreshing: false };
    } };
}

function serializeAlertLevel(level) {
    if (!level) return level;
    return { ...level, price_eur: Number(level.price_eur), triggered_at: toISODateTime(level.triggered_at), retired_at: toISODateTime(level.retired_at), created_at: toISODateTime(level.created_at), updated_at: toISODateTime(level.updated_at) };
}

function alertLevelInput(body, existing = null) {
    if (existing?.trigger_mode === 'PERCENT') {
        const input = {};
        if (body.change_pct !== undefined) input.change_pct = numberField(body.change_pct, 'Percentage move', { minimum: Number.EPSILON });
        return input;
    }
    const side = String(body.side ?? existing?.side ?? '').toUpperCase();
    if (!['BUY', 'SELL'].includes(side)) throw new Error('Side must be BUY or SELL');
    return { side, price_eur: numberField(body.price_eur ?? existing?.price_eur, 'Alert price', { minimum: Number.EPSILON }) };
}

function booleanField(value, label) {
    if ([true, 1, '1', 'true'].includes(value)) return 1;
    if ([false, 0, '0', 'false'].includes(value)) return 0;
    throw new Error(`${label} must be boolean`);
}

function serializeTrade(trade) {
    const calculated = calculateTrade(trade);
    return {
        ...calculated,
        opened_at: toISODateTime(calculated.opened_at),
        closed_at: toISODateTime(calculated.closed_at),
        created_at: toISODateTime(calculated.created_at),
        updated_at: toISODateTime(calculated.updated_at)
    };
}

function toDatabaseDate(value, fieldName) {
    if (!value || typeof value !== 'string') throw new Error(`${fieldName} is required`);
    const databaseMatch = value.match(/^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})$/);
    if (databaseMatch) {
        if (Number.isNaN(new Date(`${databaseMatch[1]}T${databaseMatch[2]}Z`).getTime())) throw new Error(`${fieldName} must be a valid date/time`);
        return value;
    }
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) throw new Error(`${fieldName} must be a valid date/time`);
    return date.toISOString().replace('T', ' ').replace(/\.\d{3}Z$/, '');
}

function numberField(value, fieldName, { minimum = 0, required = true } = {}) {
    if ((value === undefined || value === null || value === '') && !required) return 0;
    const number = Number(value);
    if (!Number.isFinite(number) || number < minimum) throw new Error(`${fieldName} must be ${minimum > 0 ? 'greater than zero' : 'zero or greater'}`);
    return number;
}

function normaliseTrade(body, existing = null) {
    const currency = String(body.currency ?? existing?.currency ?? 'EUR').trim().toUpperCase();
    if (!/^[A-Z]{3}$/.test(currency)) throw new Error('Currency must be a three-letter code');
    const entryPrice = numberField(body.entry_price ?? existing?.entry_price, 'Entry price', { minimum: Number.EPSILON });
    const entryFxRate = numberField(body.entry_fx_rate ?? existing?.entry_fx_rate ?? (currency === 'EUR' ? 1 : undefined), 'Entry EUR conversion rate', { minimum: Number.EPSILON });
    const closeRequested = body.closed_at !== undefined ? body.closed_at : existing?.closed_at;
    const closedAt = closeRequested ? toDatabaseDate(closeRequested, 'Close date/time') : null;
    const openedAt = toDatabaseDate(body.opened_at ?? existing?.opened_at, 'Open date/time');
    if (closedAt && new Date(`${closedAt}Z`) < new Date(`${openedAt}Z`)) throw new Error('Close date/time cannot be before open date/time');
    const exitPrice = closedAt ? numberField(body.exit_price ?? existing?.exit_price, 'Exit price', { minimum: Number.EPSILON }) : null;
    const exitFxRate = closedAt ? numberField(body.exit_fx_rate ?? existing?.exit_fx_rate ?? (currency === 'EUR' ? 1 : undefined), 'Exit EUR conversion rate', { minimum: Number.EPSILON }) : null;
    return {
        trade_id: existing?.trade_id || String(body.trade_id || `OST-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`).trim(),
        stock_id: Number(body.stock_id ?? existing?.stock_id),
        opened_at: openedAt,
        closed_at: closedAt,
        quantity: numberField(body.quantity ?? existing?.quantity, 'Quantity', { minimum: Number.EPSILON }),
        currency,
        entry_price: entryPrice,
        entry_fx_rate: entryFxRate,
        entry_price_eur: entryPrice * entryFxRate,
        entry_fee_eur: numberField(body.entry_fee_eur ?? existing?.entry_fee_eur ?? 1, 'Entry fee'),
        exit_price: exitPrice,
        exit_fx_rate: exitFxRate,
        exit_price_eur: closedAt ? exitPrice * exitFxRate : null,
        exit_fee_eur: closedAt ? numberField(body.exit_fee_eur ?? (existing?.closed_at ? existing.exit_fee_eur : undefined) ?? 1, 'Exit fee') : 0,
        dividend_eur: closedAt ? numberField(body.dividend_eur ?? existing?.dividend_eur ?? 0, 'Dividends received') : 0,
        strategy: String(body.strategy ?? existing?.strategy ?? '').trim(),
        notes: String(body.notes ?? existing?.notes ?? '').trim()
    };
}

function dashboardData(db) {
    return calculateDashboard({
        stocks: db.getAllStocks(),
        trades: db.getAllTrades(),
        capitalEvents: db.getCapitalEvents(),
        snapshots: db.getEquitySnapshots()
    });
}

function captureEquitySnapshot(db, source = 'dashboard') {
    const analytics = dashboardData(db);
    if (!analytics.equity_available) return null;
    const at = snapshotBucketAt(new Date());
    db.addEquitySnapshot({
        snapshot_at: at,
        cash_eur: analytics.capital.available_cash_eur,
        position_value_eur: analytics.capital.invested_eur,
        equity_eur: analytics.capital.current_eur,
        net_contributions_eur: analytics.capital.net_contributions_eur || 0,
        financing_eur: analytics.capital.financing_eur || 0,
        source
    });
    return at;
}

function queueStockHistory(historyBackfill, stock) {
    try {
        historyBackfill.enqueue(stock);
    } catch (error) {
        console.error(`Unable to queue history for ${stock?.symbol || 'stock'}:`, error.message);
    }
}

function brokerError(res, error) {
    return res.status(error instanceof BrokerError ? error.status : 502).json({ error: error.message || 'Broker unavailable', code: error.code || 'BROKER_ERROR' });
}

function createApp({ db: suppliedDb, dbPath = process.env.DATABASE_PATH || '/opt/openstock/database/openstock.db', fetchImpl = globalThis.fetch, backfill = null, sdkClient = null, now = () => new Date() } = {}) {
    const db = suppliedDb || new OpenStockDB(dbPath);
    const historyBackfill = backfill || new HistoryBackfillQueue({
        db,
        delayMs: process.env.HISTORY_BACKFILL_DELAY_MS || 1500,
    });
    const app = express();
    // OpenStock is published behind the OpenClaw reverse proxy.  Express sees
    // the final hop as HTTP unless it trusts that proxy, while browsers send
    // their HTTPS Origin header.  Trusting one proxy hop lets req.protocol use
    // X-Forwarded-Proto for the same-origin mutation check below.
    app.set('trust proxy', 1);
    app.locals.db = db;
    app.locals.historyBackfill = historyBackfill;
    app.locals.fetchImpl = fetchImpl;
    app.locals.brokerClient = new TradeRepublicClient({
        fetchImpl,
        timeoutMs: Number(process.env.TRADE_REPUBLIC_TIMEOUT_MS || 30000)
    });
    app.locals.sdkClient = sdkClient || new HandelsrepublikQuoteClient();
    historyBackfill.brokerClient = app.locals.brokerClient;
    historyBackfill.sdkClient = app.locals.sdkClient;
    const serializeConfiguredStock = stock => serializeBrokerStock(stock, db);
    app.use(express.json());
    app.use(express.urlencoded({ extended: false }));
    app.use('/api', (req, res, next) => {
    res.set({
        'Cache-Control': 'no-store, no-cache, must-revalidate, proxy-revalidate',
        Pragma: 'no-cache',
        Expires: '0',
        'X-OpenStock-Version': APP_VERSION
    });
    const forwardedHost = req.get('x-forwarded-host')?.split(',')[0].trim();
    const requestHost = forwardedHost || req.get('host');
    if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(req.method) && req.get('origin') && req.get('origin') !== `${req.protocol}://${requestHost}`) {
        return res.status(403).json({ error: 'Same-origin mutation required' });
    }
    next();
    });
    app.use(express.static(path.join(__dirname, 'www')));
    app.locals.liveQuoteFeed = createLiveQuoteFeed({ app, now });

require('./server/routes/stocks')({ app, serializeConfiguredStock, serializeAlertLevel, alertLevelInput, numberField, booleanField, queueStockHistory, resolveVerifiedInstrument, resolveCryptoCandidates, resolveVerifiedCryptoInstrument, brokerError, now, berlinTradingDayStart, berlinTradingDayEnd, evenlySample, toISODateTime });

require('./server/routes/journal')({ app, serializeTrade, normaliseTrade, numberField, toDatabaseDate, dashboardData, captureEquitySnapshot, toISODateTime });

require('./server/routes/dashboard')({ app, dashboardData, captureEquitySnapshot, serializeConfiguredStock, quoteServiceFor, now });

require('./server/routes/trade-republic')({ app, resolveVerifiedInstrument, serializeConfiguredStock, brokerError, serializeTrade });

// Health check
app.get('/health', (req, res) => {
    res.set({ 'Cache-Control': 'no-store, no-cache, must-revalidate, proxy-revalidate', Pragma: 'no-cache', Expires: '0', 'X-OpenStock-Version': APP_VERSION });
    res.json({ status: 'ok', version: APP_VERSION, timestamp: new Date().toISOString() });
});

// Serve index.html for all other routes (SPA fallback)
app.get('/', (req, res) => {
    res.sendFile(path.join(__dirname, 'www', 'index.html'));
});

// Seed charts in the background. Queue failures are isolated from API startup.
try {
    historyBackfill.enqueueMissing();
} catch (error) {
    console.error('Unable to queue startup history backfill:', error.message);
}
    return app;
}

if (require.main === module) {
    const dbPath = process.env.DATABASE_PATH || '/opt/openstock/database/openstock.db';
    const port = process.env.PORT || 3000;
    initDatabase({ dbPath });
    const app = createApp({ dbPath });
    const server = app.listen(port, '127.0.0.1', () => console.log(`OpenStock server listening on 127.0.0.1:${port}`));
    const shutdown = () => { console.log('OpenStock shutting down...'); server.close(() => { app.locals.db.db.close(); process.exit(0); }); };
    process.on('SIGTERM', shutdown);
    process.on('SIGINT', shutdown);
}

module.exports = { createApp, serializeStock, serializeTrade, berlinTradingDayStart, berlinTradingDayEnd, createLiveQuoteFeed };
