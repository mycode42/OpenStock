require('dotenv').config();
const express = require('express');
const path = require('path');
const crypto = require('node:crypto');
const OpenStockDB = require('./database/db');
const { initDatabase } = require('./database/init');
const { calculateTrade, calculateDashboard, snapshotBucketAt } = require('./lib/analytics');
const { HistoryBackfillQueue } = require('./lib/history-backfill');
const { TradeRepublicClient, sanitizeQuote, sanitizeInstrument, sanitizeAccount, sanitizeCollection, BrokerError } = require('./lib/brokers/trade-republic');
const { resolveVerifiedInstrument } = require('./lib/instrument-resolution');
const { fetchYahooFallbackQuote } = require('./lib/monitor');
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
    const brokerPriceIsCurrent = Boolean(quote && observation?.source?.startsWith('trade_republic'));
    const safeQuote = brokerPriceIsCurrent ? { bid_eur: quote.bid_eur, ask_eur: quote.ask_eur, spread_eur: quote.bid_eur !== null && quote.ask_eur !== null ? quote.ask_eur - quote.bid_eur : null, quote_time: quote.quote_time, observed_at: quote.observed_at, source: quote.source } : null;
    const fallback = stock.price_source === 'trade_republic' && Boolean(observation && !observation.source.startsWith('trade_republic'));
    const marketLabel = !observation ? 'No quote' : fallback ? 'Yahoo Finance fallback' : observation.source.startsWith('trade_republic') ? 'Trade Republic' : 'Yahoo Finance';
    return { ...serializeStock(stock), broker_mapping: mapping ? { broker: mapping.broker, ticker: mapping.ticker, isin: mapping.isin, exchange: mapping.exchange, currency: mapping.currency, verified_at: mapping.verified_at, source: mapping.source } : null, broker_quote: safeQuote,
        market_data: { source: observation?.source || null, label: marketLabel, fallback, quote_time: toISODateTime(observation?.quote_time) },
        position: db.getPositionSummary(stock.id), alert_levels: db.getAlertLevels(stock.id).map(serializeAlertLevel) };
}

function serializeAlertLevel(level) {
    if (!level) return level;
    return { ...level, price_eur: Number(level.price_eur), triggered_at: toISODateTime(level.triggered_at), retired_at: toISODateTime(level.retired_at), created_at: toISODateTime(level.created_at), updated_at: toISODateTime(level.updated_at) };
}

function alertLevelInput(body, existing = null) {
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
        entry_fee_eur: numberField(body.entry_fee_eur ?? existing?.entry_fee_eur ?? 0, 'Entry fee'),
        exit_price: exitPrice,
        exit_fx_rate: exitFxRate,
        exit_price_eur: closedAt ? exitPrice * exitFxRate : null,
        exit_fee_eur: closedAt ? numberField(body.exit_fee_eur ?? existing?.exit_fee_eur ?? 0, 'Exit fee') : 0,
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

function createApp({ db: suppliedDb, dbPath = process.env.DATABASE_PATH || '/opt/openstock/database/openstock.db', fetchImpl = globalThis.fetch, backfill = null, now = () => new Date() } = {}) {
    const db = suppliedDb || new OpenStockDB(dbPath);
    const historyBackfill = backfill || new HistoryBackfillQueue({
        db,
        delayMs: process.env.HISTORY_BACKFILL_DELAY_MS || 1500,
        range: process.env.HISTORY_RANGE || '5y',
        fetchImpl
    });
    const app = express();
    app.locals.db = db;
    app.locals.historyBackfill = historyBackfill;
    app.locals.fetchImpl = fetchImpl;
    app.locals.brokerClient = new TradeRepublicClient({
        fetchImpl,
        timeoutMs: Number(process.env.TRADE_REPUBLIC_TIMEOUT_MS || 30000)
    });
    historyBackfill.brokerClient = app.locals.brokerClient;
    const serializeConfiguredStock = stock => serializeBrokerStock(stock, db);
    app.use(express.json());
    app.use(express.urlencoded({ extended: false }));
    app.use('/api', (req, res, next) => {
    if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(req.method) && req.headers.origin) {
        try { if (new URL(req.headers.origin).host !== req.headers.host) return res.status(403).json({ error: 'Same-origin mutation required' }); }
        catch { return res.status(403).json({ error: 'Invalid mutation origin' }); }
    }
    res.set({
        'Cache-Control': 'no-store, no-cache, must-revalidate, proxy-revalidate',
        Pragma: 'no-cache',
        Expires: '0',
        'X-OpenStock-Version': APP_VERSION
    });
    next();
    });
    app.use(express.static(path.join(__dirname, 'www')));

// ===== API: Stocks =====
app.get('/api/stocks', (req, res) => {
    try {
        const stocks = req.query.includeArchived === '1' ? req.app.locals.db.getJournalStocks() : req.app.locals.db.getConfigStocks();
        res.json(stocks.map(serializeConfiguredStock));
    } catch (e) {
        console.error('Error fetching stocks:', e.message);
        res.status(500).json({ error: 'Failed to fetch stocks' });
    }
});

app.get('/api/stocks/archived', (req, res) => {
    try {
        res.json(req.app.locals.db.getArchivedStocks().map(serializeConfiguredStock));
    } catch (e) {
        console.error('Error fetching archived stocks:', e.message);
        res.status(500).json({ error: 'Failed to fetch archived stocks' });
    }
});

app.get('/api/stocks/:id/history', (req, res) => {
    const windows = {
        '1d': { since: '-2 days', queryBucketSeconds: 60, bucketSeconds: 900, maxPoints: 48, maxLiveGapSeconds: 1800 },
        '1mo': { since: '-1 month', bucketSeconds: 28800, maxPoints: 72, maxLiveGapSeconds: 43200 },
        '1y': { since: '-1 year', bucketSeconds: 345600, maxPoints: 96, maxLiveGapSeconds: 172800 },
        '5y': { since: '-5 years', bucketSeconds: 1814400, maxPoints: 96, maxLiveGapSeconds: 1209600 },
        // Keep the former endpoint working for bookmarked API consumers.
        '30d': { since: '-30 days', bucketSeconds: 28800, maxPoints: 72, maxLiveGapSeconds: 43200 }
    };
    const range = req.query.range || '30d';
    const window = windows[range];

    if (!window) {
        return res.status(400).json({ error: 'Range must be one of: 1d, 1mo, 1y, 5y' });
    }

    try {
        const stock = req.app.locals.db.getStockById(req.params.id);
        if (!stock) return res.status(404).json({ error: 'Stock not found' });
        const mapping = req.app.locals.db.getBrokerMapping(stock.id);
        const nativeTradeRepublicEur = Boolean(mapping?.verified_at && mapping.currency === 'EUR');
        const tradeRepublicSources = ['trade_republic_history', 'trade_republic_order_price', 'manual_refresh_trade_republic_order_price'];
        const tradingDayStart = range === '1d' ? berlinTradingDayStart(now()) : null;
        const eligiblePoints = req.app.locals.db.getPriceHistory(stock.id, window.since, window.queryBucketSeconds || window.bucketSeconds, {
            sources: nativeTradeRepublicEur ? tradeRepublicSources : null,
            eurOnly: true
        }).map(point => ({ ...point, recorded_at: toISODateTime(point.recorded_at), quote_time: toISODateTime(point.quote_time) }))
            .filter(point => !tradingDayStart || Date.parse(point.recorded_at) >= tradingDayStart.getTime());
        const points = evenlySample(eligiblePoints, window.maxPoints);
        res.json({ stock_id: stock.id, symbol: stock.symbol, isin: stock.isin || null, range, currency: 'EUR', native_eur: nativeTradeRepublicEur, source: nativeTradeRepublicEur ? 'trade_republic' : 'local_eur', bucket_seconds: window.bucketSeconds, max_gap_seconds: window.maxLiveGapSeconds, start_at: tradingDayStart?.toISOString() || null, points });
    } catch (e) {
        console.error('Error fetching price history:', e.message);
        res.status(500).json({ error: 'Failed to fetch price history' });
    }
});

app.get('/api/stocks/:id', (req, res) => {
    try {
        const stock = req.app.locals.db.getStockById(req.params.id);
        if (!stock) {
            return res.status(404).json({ error: 'Stock not found' });
        }
        res.json(serializeConfiguredStock(stock));
    } catch (e) {
        console.error('Error fetching stock:', e.message);
        res.status(500).json({ error: 'Failed to fetch stock' });
    }
});

app.get('/api/stocks/:id/alert-levels', (req, res) => {
    try {
        const stock = req.app.locals.db.getStockById(req.params.id);
        if (!stock) return res.status(404).json({ error: 'Stock not found' });
        res.json(req.app.locals.db.getAlertLevels(stock.id).map(serializeAlertLevel));
    } catch (e) { res.status(500).json({ error: 'Failed to fetch alert levels' }); }
});

app.post('/api/stocks/:id/alert-levels', (req, res) => {
    try {
        const stock = req.app.locals.db.getStockById(req.params.id);
        if (!stock) return res.status(404).json({ error: 'Stock not found' });
        const input = alertLevelInput(req.body);
        const level = req.app.locals.db.addAlertLevel(stock.id, input.side, input.price_eur);
        res.status(201).json({ alert_level: serializeAlertLevel(level) });
    } catch (e) { res.status(e.code === 'SQLITE_CONSTRAINT_UNIQUE' ? 409 : 400).json({ error: e.message || 'Invalid alert level' }); }
});

app.put('/api/alert-levels/:id', (req, res) => {
    try {
        const existing = req.app.locals.db.getAlertLevelById(req.params.id);
        if (!existing) return res.status(404).json({ error: 'Alert level not found' });
        const input = alertLevelInput(req.body, existing);
        for (const key of ['enabled', 'active']) if (req.body[key] !== undefined) input[key] = booleanField(req.body[key], key);
        res.json({ alert_level: serializeAlertLevel(req.app.locals.db.updateAlertLevel(existing.id, input)) });
    } catch (e) { res.status(e.code === 'SQLITE_CONSTRAINT_UNIQUE' ? 409 : 400).json({ error: e.message || 'Invalid alert level' }); }
});

app.post('/api/alert-levels/:id/rearm', (req, res) => {
    const existing = req.app.locals.db.getAlertLevelById(req.params.id);
    if (!existing) return res.status(404).json({ error: 'Alert level not found' });
    res.json({ alert_level: serializeAlertLevel(req.app.locals.db.rearmAlertLevel(existing.id)) });
});

app.delete('/api/alert-levels/:id', (req, res) => {
    const existing = req.app.locals.db.getAlertLevelById(req.params.id);
    if (!existing) return res.status(404).json({ error: 'Alert level not found' });
    req.app.locals.db.deleteAlertLevel(existing.id);
    res.json({ deleted_id: existing.id });
});

app.post('/api/instruments/resolve', async (req, res) => {
    try {
        const result = await resolveVerifiedInstrument(req.app.locals.brokerClient, req.body?.isin);
        res.json({ verified: true, ...result });
    } catch (e) { if (e.status) return res.status(e.status).json({ error: e.message, verified: false }); brokerError(res, e); }
});

app.post('/api/stocks', async (req, res) => {
    try {
        const isin = String(req.body?.isin || '').trim().toUpperCase();
        const buyThreshold = Number(req.body?.buy_threshold_eur), sellThreshold = Number(req.body?.sell_threshold_eur);
        if (!/^[A-Z0-9]{12}$/.test(isin)) return res.status(400).json({ error: 'A valid 12-character ISIN is required' });
        if (!(buyThreshold > 0) || !(sellThreshold > 0) || buyThreshold >= sellThreshold) return res.status(400).json({ error: 'Buy and sell thresholds must be positive and BUY must be below SELL' });
        const { instrument, home } = await resolveVerifiedInstrument(req.app.locals.brokerClient, isin);
        const exchange = home.exchange, currency = home.currency;
        const existingIsin = req.app.locals.db.getConfigStocks().find(stock => stock.isin === isin) || req.app.locals.db.getArchivedStocks().find(stock => stock.isin === isin);
        if (existingIsin) return res.status(409).json({ error: existingIsin.archived ? 'ISIN belongs to an archived stock; restore that identity explicitly' : 'ISIN already exists', stock_id: existingIsin.id });
        if (req.app.locals.db.getActiveStocks().length >= 25) return res.status(400).json({ error: 'Maximum 25 active stocks allowed' });
        const symbol = instrument.ticker;
        const existingSymbol = req.app.locals.db.getStockBySymbol(symbol);
        if (existingSymbol) return res.status(409).json({ error: existingSymbol.archived ? 'Derived symbol belongs to an archived stock; restore that identity explicitly' : 'Derived symbol already exists' });
        const mapping = { broker: 'trade_republic', ticker: symbol, isin, exchange, currency, broker_metadata_json: JSON.stringify({ instrument, home }), source: 'broker_verified', verified_at: new Date().toISOString() };
        const result = req.app.locals.db.addStockWithMapping({ symbol, name: instrument.name, isin, currency, buyThreshold, sellThreshold, mapping });
        const stock = req.app.locals.db.getStockById(result.lastInsertRowid); queueStockHistory(req.app.locals.historyBackfill, stock);
        res.status(201).json({ message: 'Stock added from verified Trade Republic instrument', stock: serializeConfiguredStock(stock), mapping: req.app.locals.db.getBrokerMapping(stock.id) });
    } catch (e) { if (e.status) return res.status(e.status).json({ error: e.message }); if (e.code === 'SQLITE_CONSTRAINT') return res.status(409).json({ error: 'Stock ISIN or derived symbol already exists' }); console.error('Error adding stock:', e.message); res.status(500).json({ error: 'Failed to add stock' }); }
});

app.put('/api/stocks/:id', (req, res) => {
    try {
        const { buy_threshold_eur, sell_threshold_eur, owned, enabled, name, price_source } = req.body;
        if (buy_threshold_eur !== undefined || sell_threshold_eur !== undefined) {
            return res.status(400).json({ error: 'Existing-stock threshold changes are no longer accepted; manage alert levels instead.' });
        }
        const updates = {};

        if (owned !== undefined) {
            return res.status(400).json({ error: 'Manual ownership is no longer supported; use Journal positions and rearm alert levels.' });
        }

        if (enabled !== undefined) {
            const stock = req.app.locals.db.getStockById(req.params.id);
            if (!stock) {
                return res.status(404).json({ error: 'Stock not found' });
            }
            const newEnabled = enabled === 1 || enabled === '1' || enabled === true || enabled === 'true';
            if (newEnabled && Number(stock.archived)) {
                return res.status(400).json({ error: 'Archived stocks must be restored with the restore action' });
            }
            if (newEnabled && !Number(stock.enabled) && req.app.locals.db.getActiveStocks().length >= 25) {
                return res.status(400).json({ error: 'Maximum 25 active stocks allowed' });
            }
            updates.enabled = newEnabled ? 1 : 0;
        }

        if (name !== undefined) {
            if (!name || typeof name !== 'string' || name.trim() === '') {
                return res.status(400).json({ error: 'Invalid name' });
            }
            updates.name = name.trim();
        }
        if (price_source !== undefined) {
            if (!['yahoo', 'trade_republic'].includes(String(price_source))) return res.status(400).json({ error: 'Price source must be yahoo or trade_republic' });
            updates.price_source = String(price_source);
        }

        if (Object.keys(updates).length === 0) {
            return res.status(400).json({ error: 'No valid fields to update' });
        }

        const result = req.app.locals.db.updateStock(req.params.id, updates);
        if (result.changes === 0) {
            return res.status(404).json({ error: 'Stock not found' });
        }
        const stock = req.app.locals.db.getStockById(req.params.id);
        res.json({ message: 'Stock updated successfully', stock: serializeConfiguredStock(stock) });
    } catch (e) {
        console.error('Error updating stock:', e.message);
        res.status(500).json({ error: 'Failed to update stock' });
    }
});

app.post('/api/stocks/:id/restore', (req, res) => {
    try {
        const existing = req.app.locals.db.getStockById(req.params.id);
        if (!existing || !Number(existing.archived)) return res.status(404).json({ error: 'Archived stock not found' });
        if (req.app.locals.db.getActiveStocks().length >= 25) return res.status(400).json({ error: 'Maximum 25 active stocks allowed' });
        const name = String(req.body.name ?? existing.name ?? '').trim();
        const currency = String(req.body.currency ?? existing.currency ?? 'USD').trim().toUpperCase();
        const buyThreshold = numberField(req.body.buy_threshold_eur ?? existing.buy_threshold_eur, 'Buy threshold', { minimum: Number.EPSILON });
        const sellThreshold = numberField(req.body.sell_threshold_eur ?? existing.sell_threshold_eur, 'Sell threshold', { minimum: Number.EPSILON });
        if (!name) return res.status(400).json({ error: 'Name is required' });
        if (!/^[A-Z]{3}$/.test(currency)) return res.status(400).json({ error: 'Currency must be a three-letter code' });
        if (buyThreshold >= sellThreshold) return res.status(400).json({ error: 'Buy threshold must be less than sell threshold' });
        req.app.locals.db.restoreArchivedStock(existing.id, { name, currency, buyThreshold, sellThreshold });
        const stock = req.app.locals.db.getStockById(existing.id);
        queueStockHistory(req.app.locals.historyBackfill, stock);
        res.json({ message: 'Stock restored successfully', restored: true, stock: serializeConfiguredStock(stock) });
    } catch (e) {
        console.error('Error restoring stock:', e.message);
        res.status(400).json({ error: e.message || 'Failed to restore stock' });
    }
});

app.delete('/api/stocks/:id', (req, res) => {
    try {
        const result = req.app.locals.db.removeStock(req.params.id);
        if (result.changes === 0) {
            return res.status(404).json({ error: 'Stock not found' });
        }
        res.json({ message: result.archived ? 'Stock archived; journal history retained' : 'Stock deleted successfully', deleted_id: Number(req.params.id), archived: result.archived });
    } catch (e) {
        console.error('Error deleting stock:', e.message);
        res.status(500).json({ error: 'Failed to delete stock' });
    }
});

// ===== API: Trades, capital events and performance =====
app.get('/api/trades', (req, res) => {
    try {
        const status = ['open', 'closed'].includes(req.query.status) ? req.query.status : undefined;
        const symbol = req.query.symbol ? String(req.query.symbol).toUpperCase() : undefined;
        res.json(req.app.locals.db.getAllTrades({ status, symbol }).map(serializeTrade));
    } catch (e) {
        console.error('Error fetching trades:', e.message);
        res.status(500).json({ error: 'Failed to fetch trades' });
    }
});

app.post('/api/trades', (req, res) => {
    try {
        const trade = normaliseTrade(req.body);
        if (!Number.isInteger(trade.stock_id) || !req.app.locals.db.getStockById(trade.stock_id)) return res.status(400).json({ error: 'A valid stock is required' });
        const result = req.app.locals.db.addTrade(trade);
        captureEquitySnapshot(req.app.locals.db, result.merged ? 'trade_merge' : 'trade_create');
        res.status(201).json({ message: result.merged ? 'Purchase merged into the existing open position' : 'Trade recorded', merged: result.merged, merged_count: result.mergedCount, trade: serializeTrade(req.app.locals.db.getTradeById(result.lastInsertRowid)) });
    } catch (e) {
        if (e.code === 'SQLITE_CONSTRAINT_UNIQUE') return res.status(409).json({ error: 'Trade ID already exists' });
        res.status(400).json({ error: e.message || 'Invalid trade' });
    }
});

app.put('/api/trades/:id', (req, res) => {
    try {
        const existing = req.app.locals.db.getTradeById(req.params.id);
        if (!existing) return res.status(404).json({ error: 'Trade not found' });
        const trade = normaliseTrade(req.body, existing);
        if (!Number.isInteger(trade.stock_id) || !req.app.locals.db.getStockById(trade.stock_id)) return res.status(400).json({ error: 'A valid stock is required' });
        req.app.locals.db.updateTrade(existing.id, trade);
        captureEquitySnapshot(req.app.locals.db, 'trade_edit');
        res.json({ message: 'Trade updated explicitly', trade: serializeTrade(req.app.locals.db.getTradeById(existing.id)) });
    } catch (e) {
        res.status(400).json({ error: e.message || 'Invalid trade' });
    }
});

app.post('/api/trades/:id/close', (req, res) => {
    try {
        const existing = req.app.locals.db.getTradeById(req.params.id);
        if (!existing) return res.status(404).json({ error: 'Trade not found' });
        if (existing.closed_at) return res.status(409).json({ error: 'Trade is already closed; use explicit edit to correct it' });
        const trade = normaliseTrade({ ...req.body, closed_at: req.body.closed_at || new Date().toISOString() }, existing);
        req.app.locals.db.updateTrade(existing.id, trade);
        captureEquitySnapshot(req.app.locals.db, 'trade_close');
        res.json({ message: 'Trade closed', trade: serializeTrade(req.app.locals.db.getTradeById(existing.id)) });
    } catch (e) {
        res.status(400).json({ error: e.message || 'Invalid close details' });
    }
});

app.get('/api/capital-events', (req, res) => {
    try {
        res.json(req.app.locals.db.getCapitalEvents().map(event => ({ ...event, occurred_at: toISODateTime(event.occurred_at), created_at: toISODateTime(event.created_at) })));
    } catch (e) {
        res.status(500).json({ error: 'Failed to fetch capital events' });
    }
});

app.post('/api/capital-events', (req, res) => {
    try {
        const event_type = String(req.body.event_type || '').toUpperCase();
        if (!['INITIAL', 'DEPOSIT', 'WITHDRAWAL', 'FINANCING'].includes(event_type)) throw new Error('Event type must be INITIAL, DEPOSIT, WITHDRAWAL, or FINANCING');
        const event = { event_type, amount_eur: numberField(req.body.amount_eur, 'Amount', { minimum: Number.EPSILON }), occurred_at: toDatabaseDate(req.body.occurred_at || new Date().toISOString(), 'Event date/time'), notes: String(req.body.notes || '').trim() };
        if (event_type === 'INITIAL' && req.app.locals.db.getCapitalEvents().some(item => item.event_type === 'INITIAL')) throw new Error('An initial capital event already exists; add a deposit instead');
        const result = req.app.locals.db.addCapitalEvent(event);
        captureEquitySnapshot(req.app.locals.db, 'capital_event');
        const saved = req.app.locals.db.getCapitalEvents().find(item => item.id === Number(result.lastInsertRowid));
        res.status(201).json({ message: 'Capital event recorded', event: { ...saved, occurred_at: toISODateTime(saved.occurred_at), created_at: toISODateTime(saved.created_at) } });
    } catch (e) {
        res.status(400).json({ error: e.message || 'Invalid capital event' });
    }
});

app.get('/api/analytics/dashboard', (req, res) => {
    try {
        captureEquitySnapshot(req.app.locals.db, 'dashboard');
        res.json(dashboardData(req.app.locals.db));
    } catch (e) {
        console.error('Error calculating dashboard:', e.message);
        res.status(500).json({ error: 'Failed to calculate dashboard' });
    }
});

// ===== API: Dashboard data =====
app.get('/api/dashboard', (req, res) => {
    try {
        const stocks = req.app.locals.db.getActiveStocks();
        res.json({
            stocks: stocks.map(serializeConfiguredStock),
            analytics: dashboardData(req.app.locals.db)
        });
    } catch (e) {
        console.error('Error fetching dashboard:', e.message);
        res.status(500).json({ error: 'Failed to fetch dashboard data' });
    }
});

// ===== API: Refresh Prices =====
app.post('/api/refresh-prices', async (req, res) => {
    try {
        // Get all enabled stocks
        const stocks = req.app.locals.db.getEnabledStocks();
        if (stocks.length === 0) {
            return res.json({
                message: 'No enabled stocks to refresh',
                updated: 0,
                stocks: req.app.locals.db.getAllStocks().map(serializeConfiguredStock)
            });
        }
        
        let updatedCount = 0;
        
        // Process each stock
        for (const stock of stocks) {
            try {
                const mapping = req.app.locals.db.getBrokerMapping(stock.id);
                if (mapping?.verified_at && mapping.currency === 'EUR') {
                    try {
                        const brokerQuote = await req.app.locals.brokerClient.getQuote({ isin: mapping.isin, exchange: mapping.exchange });
                        const eurPrice = Number.isFinite(brokerQuote.bid_eur) && Number.isFinite(brokerQuote.ask_eur) ? (brokerQuote.bid_eur + brokerQuote.ask_eur) / 2 : brokerQuote.bid_eur || brokerQuote.ask_eur;
                        if (Number.isFinite(eurPrice) && eurPrice > 0 && brokerQuote.currency === 'EUR') {
                            req.app.locals.db.updateStockQuote(stock.id, { bidEur: brokerQuote.bid_eur, askEur: brokerQuote.ask_eur, midpointEur: eurPrice, quoteTime: brokerQuote.quote_time });
                            req.app.locals.db.addPriceSnapshot(stock.id, null, eurPrice, null, 'manual_refresh_trade_republic_order_price', brokerQuote.quote_time);
                            req.app.locals.db.addBrokerQuote({ broker:'trade_republic', isin:mapping.isin, exchange:mapping.exchange, bid_eur:brokerQuote.bid_eur, ask_eur:brokerQuote.ask_eur, last_eur:brokerQuote.last_eur || null, currency:brokerQuote.currency, quote_time:brokerQuote.quote_time, observed_at:brokerQuote.observed_at, source:brokerQuote.source });
                            updatedCount++; continue;
                        }
                    } catch (brokerError) { console.warn(`Trade Republic quote unavailable for ${stock.symbol}; Yahoo fallback: ${brokerError.code || 'UNAVAILABLE'}`); }
                }
                const quote = await fetchYahooFallbackQuote({
                    symbol: stock.symbol,
                    yahooUrl: process.env.YAHOO_CHART_URL || 'https://query1.finance.yahoo.com/v8/finance/chart',
                    fx: {
                        cachePath: process.env.FX_CACHE_FILE || path.join(__dirname, 'memory/exchange_rate_cache.json'),
                        ttlMs: Number(process.env.EXCHANGE_RATE_CACHE_SECONDS || 3600) * 1000,
                        url: process.env.FX_URL || 'https://api.exchangerate-api.com/v4/latest/USD'
                    },
                    fetchImpl: req.app.locals.fetchImpl,
                    timeoutMs: Number(process.env.PRICE_FETCH_TIMEOUT_MS || 10000),
                    now: Date.now(),
                    log: message => console.warn(message)
                });
                req.app.locals.db.updateStockPrice(stock.id, quote.priceUsd, quote.priceEur, quote.exchangeRate);
                req.app.locals.db.addPriceSnapshot(stock.id, quote.priceUsd, quote.priceEur, quote.exchangeRate, `manual_refresh_${quote.source}`, quote.quoteTime);
                updatedCount++;
                if ((!stock.name || stock.name === '') && quote.name) req.app.locals.db.updateStock(stock.id, { name: quote.name });
            } catch (stockError) {
                console.warn(`Error processing stock ${stock.symbol}:`, stockError.message);
                continue;
            }
        }
        
        res.json({ 
            message: `Successfully refreshed ${updatedCount} stock prices`, 
            updated: updatedCount,
            stocks: req.app.locals.db.getAllStocks().map(serializeConfiguredStock)
        });
    } catch (e) {
        console.error('Error refreshing prices:', e.message);
        res.status(500).json({ error: 'Failed to refresh prices' });
    }
});

// ===== API: broker integration (sanitized, read-first) =====
function brokerError(res, error) { return res.status(error instanceof BrokerError ? error.status : 502).json({ error: error.message || 'Broker unavailable', code: error.code || 'BROKER_ERROR' }); }
function brokerRows(raw, kind) {
    const observedAt = new Date().toISOString();
    const rows = Array.isArray(raw) && !raw.some(row => row?.amount && typeof row.amount === 'object') ? raw : sanitizeCollection(raw, kind);
    return rows.map(row => {
        const base = { broker: 'trade_republic', observed_at: row.observed_at || observedAt, raw_hash: null };
        if (kind === 'portfolio') return { ...base, account_ref_masked: row.account_ref_masked || '••••', isin: row.isin || row.instrument || null, quantity: row.quantity, currency: row.currency || 'EUR', value_eur: row.value_eur ?? row.amount_eur ?? null, external_ref_masked: row.external_ref_masked || '••••' };
        if (kind === 'orders') return { ...base, external_ref_masked: row.external_ref_masked || '••••', status: row.status || 'UNKNOWN', isin: row.isin || row.instrument || null, side: row.side || null, quantity: row.quantity ?? null, amount_eur: row.amount_eur ?? null, occurred_at: row.occurred_at || null, execution_identity: row.execution_identity || null, execution_price_eur: row.execution_price_eur ?? null, execution_fees_eur: row.execution_fees_eur ?? null, executed_at: row.executed_at || null, title: row.title || null, subtitle: row.subtitle || null };
        return { ...base, external_ref_masked: row.external_ref_masked || '••••', status: row.status || 'UNKNOWN', isin: row.isin || row.instrument || null, side: row.side || null, quantity: row.quantity ?? null, amount_eur: row.amount_eur ?? null, occurred_at: row.occurred_at || null, execution_identity: row.execution_identity || null, execution_price_eur: row.execution_price_eur ?? null, execution_fees_eur: row.execution_fees_eur ?? null, executed_at: row.executed_at || null, title: row.title || null, subtitle: row.subtitle || null };
    });
}
app.get('/api/broker/trade-republic/health', async (req, res) => { try { res.json({ broker: 'trade_republic', ...(await req.app.locals.brokerClient.health()), live_submission_enabled: process.env.TRADE_REPUBLIC_LIVE_ENABLED === 'true' && process.env.TRADE_REPUBLIC_DRY_RUN === 'false' }); } catch (e) { brokerError(res, e); } });
app.get('/api/broker/trade-republic/accounts', async (req, res) => { try { res.json(await req.app.locals.brokerClient.getAccounts()); } catch(e) { brokerError(res,e); } });
app.get('/api/broker/trade-republic/portfolio', async (req, res) => { try { res.json(await req.app.locals.brokerClient.getPortfolio()); } catch(e) { brokerError(res,e); } });
app.get('/api/broker/trade-republic/orders', async (req, res) => { try { res.json(await req.app.locals.brokerClient.getOrders()); } catch(e) { brokerError(res,e); } });
app.get('/api/broker/trade-republic/transactions', async (req, res) => { try { res.json(await req.app.locals.brokerClient.getTransactions()); } catch(e) { brokerError(res,e); } });
app.get('/api/broker/trade-republic/interests', async (req, res) => { try { res.json(await req.app.locals.brokerClient.getInterests()); } catch(e) { brokerError(res,e); } });
app.get('/api/broker/trade-republic/tickets', async (req, res) => { try { res.json(await req.app.locals.brokerClient.getTickets()); } catch(e) { brokerError(res,e); } });
app.get('/api/broker/trade-republic/activity', async (req, res) => { try { res.json(await req.app.locals.brokerClient.getAccountActivity()); } catch(e) { brokerError(res,e); } });
app.get('/api/broker/trade-republic/price-alarms', async (req, res) => { try { res.json(await req.app.locals.brokerClient.getPriceAlarms()); } catch(e) { brokerError(res,e); } });
app.get('/api/broker/trade-republic/reconciliation-candidates', (req, res) => res.json(req.app.locals.db.getBrokerReconciliationCandidates()));
app.post('/api/broker/trade-republic/accounts-history', async (req, res) => { try { const range=String(req.body?.range||'1y'); if(!['1d','5d','1m','1y','max'].includes(range)) return res.status(400).json({error:'Invalid account history range'}); res.json(await req.app.locals.brokerClient.getAccountsHistory(range)); } catch(e) { brokerError(res,e); } });
app.get('/api/broker/trade-republic/instrument/:stockId', async (req, res) => { try { const mapping=req.app.locals.db.getBrokerMapping(Number(req.params.stockId)); if(!mapping?.verified_at) return res.status(409).json({error:'Verified mapping required'}); res.json(await req.app.locals.brokerClient.getInstrument(mapping.isin)); } catch(e) { brokerError(res,e); } });
app.get('/api/broker/trade-republic/tr-instrument/:stockId', async (req, res) => { try { const mapping=req.app.locals.db.getBrokerMapping(Number(req.params.stockId)); if(!mapping?.verified_at) return res.status(409).json({error:'Verified mapping required'}); res.json(await req.app.locals.brokerClient.getTradeRepublicInstrument(mapping.isin)); } catch(e) { brokerError(res,e); } });
app.get('/api/broker/trade-republic/instrument-history/:stockId', async (req, res) => { try { const mapping=req.app.locals.db.getBrokerMapping(Number(req.params.stockId)); const range=String(req.query.range||'1m'); if(!mapping?.verified_at) return res.status(409).json({error:'Verified mapping required'}); if(!['1d','5d','1m','1y','max'].includes(range)) return res.status(400).json({error:'Invalid instrument history range'}); res.json(await req.app.locals.brokerClient.getInstrumentHistory({isin:mapping.isin,range})); } catch(e) { brokerError(res,e); } });
app.get('/api/broker/trade-republic/schedule/:stockId', async (req, res) => { try { const mapping=req.app.locals.db.getBrokerMapping(Number(req.params.stockId)); if(!mapping?.verified_at) return res.status(409).json({error:'Verified mapping required'}); res.json(await req.app.locals.brokerClient.getExchangeSchedule(mapping.exchange)); } catch(e) { brokerError(res,e); } });
app.post('/api/broker/trade-republic/cancel', (req, res) => res.status(501).json({ error:'Live cancellation is disabled; no broker call was made', code:'CANCELLATION_DISABLED' }));
app.get('/api/broker/trade-republic/mappings', (req, res) => res.json(req.app.locals.db.getBrokerMappings().map(({ broker_metadata_json, ...m }) => ({ ...m, broker_metadata: JSON.parse(broker_metadata_json || '{}') }))));
app.post('/api/broker/trade-republic/mappings', async (req, res) => { try { const b=req.body||{}; const stock=req.app.locals.db.getStockById(Number(b.stock_id)); if(!stock) return res.status(404).json({error:'Stock not found'}); const { instrument, home } = await resolveVerifiedInstrument(req.app.locals.brokerClient, b.isin); const existing=req.app.locals.db.getConfigStocks().find(item => item.isin === instrument.isin && item.id !== stock.id) || req.app.locals.db.getArchivedStocks().find(item => item.isin === instrument.isin && item.id !== stock.id); if (existing) return res.status(409).json({error: existing.archived ? 'ISIN belongs to an archived stock; restore that identity explicitly' : 'ISIN already exists', stock_id: existing.id}); req.app.locals.db.saveBrokerMapping({stock_id:stock.id,broker:'trade_republic',ticker:instrument.ticker,isin:instrument.isin,exchange:home.exchange,currency:home.currency,broker_metadata_json:JSON.stringify({instrument,home}),source:'broker_verified',verified_at:new Date().toISOString()}); req.app.locals.db.updateStock(stock.id,{isin:instrument.isin,name:instrument.name,price_source:'trade_republic'}); res.status(201).json({mapping:req.app.locals.db.getBrokerMapping(stock.id), stock:serializeConfiguredStock(req.app.locals.db.getStockById(stock.id))}); } catch(e) { if (e.status) return res.status(e.status).json({error:e.message,verified:false}); brokerError(res,e); } });
app.post('/api/stocks/:id/history/sync-tr', async (req, res) => { try { const stock=req.app.locals.db.getStockById(Number(req.params.id)); if(!stock) return res.status(404).json({error:'Stock not found'}); const result=await req.app.locals.historyBackfill.syncTradeRepublic(stock); res.json({message:'Trade Republic history synchronized', ...result}); } catch(e) { res.status(e.message === 'verified Trade Republic mapping unavailable' ? 409 : 502).json({error:e.message || 'Trade Republic history unavailable'}); } });
app.get('/api/broker/trade-republic/mapping-candidates', async (req, res) => { try { const portfolio=await req.app.locals.brokerClient.getPortfolio(); res.json({candidates:portfolio.filter(x=>x.instrument).map(x=>({ticker:null,isin:x.instrument,quantity:x.quantity,account_ref_masked:x.account_ref_masked,match:'unresolved',reason:'Ticker-to-ISIN mapping requires explicit verification'}))}); } catch(e) { brokerError(res,e); } });
app.get('/api/broker/trade-republic/quote/:stockId', async (req, res) => { try { const mapping=req.app.locals.db.getBrokerMapping(Number(req.params.stockId)); if(!mapping) return res.status(409).json({error:'Stock has no verified broker mapping'}); const quote=await req.app.locals.brokerClient.getQuote({isin:mapping.isin,exchange:mapping.exchange}); const quoteId=req.app.locals.db.addBrokerQuote({broker:'trade_republic',isin:mapping.isin,exchange:mapping.exchange,bid_eur:quote.bid_eur,ask_eur:quote.ask_eur,last_eur:quote.last_eur,currency:quote.currency,quote_time:quote.quote_time,observed_at:quote.observed_at,source:quote.source}).lastInsertRowid; res.json({...quote,quote_id:Number(quoteId),fresh:Date.now()-Date.parse(quote.observed_at)<15000}); } catch(e) { brokerError(res,e); } });
app.post('/api/broker/trade-republic/sync', async (req, res) => {
  try {
    const client=req.app.locals.brokerClient, observed_at=new Date().toISOString();
    const [accounts,portfolio,orders,transactions]=await Promise.all([client.getAccounts(),client.getPortfolio(),client.getOrders(),client.getTransactions()]);
    const accountRows=accounts.filter(Boolean).map(a=>({...a,broker:'trade_republic',observed_at,raw_hash:null}));
    const orderRows=brokerRows(orders,'orders');
    const transactionRows=brokerRows(transactions,'transactions');
    const counts={accounts:req.app.locals.db.syncBrokerRows('broker_accounts',accountRows),portfolio:req.app.locals.db.syncBrokerRows('broker_portfolio',brokerRows(portfolio,'portfolio').filter(x=>x.quantity!==null)),orders:req.app.locals.db.syncBrokerRows('broker_orders',orderRows),transactions:req.app.locals.db.syncBrokerRows('broker_transactions',transactionRows)};
    const candidates=[...orderRows,...transactionRows].filter(row=>['FILLED','EXECUTED'].includes(String(row.status).toUpperCase()) && !(row.execution_identity && req.app.locals.db.getBrokerExecution(row.execution_identity))).map(row=>{
      const complete=Boolean(row.execution_identity && row.instrument && row.side && row.quantity>0 && row.execution_price_eur>0 && row.execution_fees_eur!==null && row.execution_fees_eur!==undefined && row.executed_at);
      const candidateKey=crypto.createHash('sha256').update(JSON.stringify([row.external_ref_masked||null,row.occurred_at||null,row.title||null,row.amount_eur??null])).digest('hex');
      return {broker:'trade_republic',candidate_key:candidateKey,external_ref_masked:row.external_ref_masked,status:row.status,title:row.title||null,subtitle:row.subtitle||null,amount_eur:row.amount_eur,currency:row.currency,occurred_at:row.occurred_at,reason:complete?'Execution is not linked to an OpenStock order intent; review before journaling':'Authoritative ISIN, side, quantity, execution price, fees, or execution identity is missing; complete manually before journaling'};
    });
    const newCandidates=candidates.reduce((sum,candidate)=>sum+req.app.locals.db.addBrokerReconciliationCandidate(candidate).changes,0);
    res.json({broker:'trade_republic',observed_at,counts,new_reconciliation_candidates:newCandidates,pending_reconciliation:req.app.locals.db.getBrokerReconciliationCandidates().length});
  } catch(e) { brokerError(res,e); }
});
function normalizeOrderSizing(body, sizingPrice) {
    const hasQuantity = body.quantity !== undefined && body.quantity !== null && body.quantity !== '';
    const hasAmount = body.amount_eur !== undefined && body.amount_eur !== null && body.amount_eur !== '';
    if (hasQuantity === hasAmount) return { valid: false, error: 'Enter either a quantity or a euro value' };
    if (hasQuantity) {
        const quantity = Number(body.quantity);
        return Number.isInteger(quantity) && quantity > 0
            ? { valid: true, quantity, sizing_mode: 'quantity', requested_amount_eur: null, sizing_price_eur: Number(sizingPrice) || null }
            : { valid: false, error: 'Quantity must be a positive whole number' };
    }
    const requestedAmount = Number(body.amount_eur);
    const price = Number(sizingPrice);
    if (!(Number.isFinite(requestedAmount) && requestedAmount > 0)) return { valid: false, error: 'Euro value must be positive' };
    if (!(Number.isFinite(price) && price > 0)) return { valid: false, error: 'A current EUR price is required for euro-value sizing' };
    const quantity = Math.floor(requestedAmount / price);
    return quantity > 0
        ? { valid: true, quantity, sizing_mode: 'amount', requested_amount_eur: requestedAmount, sizing_price_eur: price }
        : { valid: false, error: 'Euro value is below the price of one security' };
}

app.post('/api/broker/trade-republic/order-preview', async (req, res) => {
    try {
        const b = req.body || {};
        const db = req.app.locals.db;
        const mapping = db.getBrokerMapping(Number(b.stock_id));
        if (!mapping?.verified_at || mapping.currency !== 'EUR') return res.status(409).json({ error: 'Verified EUR mapping required' });
        const mode = String(b.mode || 'market');
        const validity = String(b.validity || 'GFD').toUpperCase();
        const limit = b.limit === '' || b.limit === undefined ? null : Number(b.limit);
        const stop = b.stop === '' || b.stop === undefined ? null : Number(b.stop);
        const valid = ['buy', 'sell'].includes(b.type) && ['market', 'limit', 'stopMarket'].includes(mode) && ['GFD', 'GTD', 'GTC'].includes(validity) && (mode !== 'limit' || limit > 0) && (mode === 'limit' || limit === null) && (mode !== 'stopMarket' || stop > 0) && (mode === 'stopMarket' || stop === null);
        if (!valid) return res.status(400).json({ error: 'Invalid mode, validity, limit, or stop' });
        const quote = await req.app.locals.brokerClient.getQuote({ isin: mapping.isin, exchange: mapping.exchange });
        const side = b.type === 'buy' ? 'ask_eur' : 'bid_eur';
        if (!quote[side]) return res.status(502).json({ error: 'Fresh side quote unavailable' });
        const referencePrice = Number(quote[side]);
        const sizingPrice = mode === 'limit' ? Math.max(referencePrice, limit) : referencePrice;
        const sizing = normalizeOrderSizing(b, sizingPrice);
        if (!sizing.valid) return res.status(400).json({ error: sizing.error });
        const quantity = sizing.quantity;
        db.addBrokerQuote({ broker: 'trade_republic', isin: mapping.isin, exchange: mapping.exchange, bid_eur: quote.bid_eur, ask_eur: quote.ask_eur, last_eur: quote.last_eur || null, currency: quote.currency, quote_time: quote.quote_time, observed_at: quote.observed_at, source: quote.source });
        const fee = await req.app.locals.brokerClient.previewFees({ account_ref_masked: b.account_ref, exchange: mapping.exchange, instrument: mapping.isin, mode: mode, quantity: quantity, type: b.type, limit: limit, stop: stop, validity });
        const feeEur = Number(fee.fee_eur);
        const estimatedNotional = quantity * sizingPrice;
        const totalEur = Number.isFinite(feeEur) ? estimatedNotional + (b.type === 'buy' ? feeEur : -feeEur) : null;
        res.json({ broker: 'trade_republic', instrument: mapping.isin, exchange: mapping.exchange, side: b.type, quantity, sizing_mode: sizing.sizing_mode, requested_amount_eur: sizing.requested_amount_eur, sizing_price_eur: sizing.sizing_price_eur, reference_price_eur: referencePrice, fee_eur: fee.fee_eur, total_eur: totalEur, quote });
    } catch (e) { brokerError(res, e); }
});

async function authoritativeExecution(result) {
    const direct = result.execution_details || (result.execution && typeof result.execution === 'object' ? result.execution : null);
    if (!direct || !direct.identity || direct.fees_eur === null || direct.fees_eur === undefined || !direct.executed_at) return null;
    if (!(Number(direct.price_eur) > 0 && Number(direct.quantity) > 0 && Number.isFinite(Number(direct.fees_eur)) && Number(direct.fees_eur) >= 0)) return null;
    return Number.isNaN(Date.parse(direct.executed_at)) ? null : direct;
}

app.post('/api/broker/trade-republic/orders', async (req,res) => {
    const b=req.body||{}, db=req.app.locals.db, client=req.app.locals.brokerClient;
    const key=String(b.idempotency_key||'');
    const existing=key ? db.getBrokerOrderIntent(key) : null;
    if(existing) return res.status(existing.status==='REJECTED'?409:200).json({order:{idempotency_key:key,status:existing.status,external_ref_masked:existing.external_ref_masked||null},duplicate:true});
    const mapping=db.getBrokerMapping(Number(b.stock_id));
    const q=mapping&&db.getLatestBrokerQuote(mapping.isin,mapping.exchange);
    const side=String(b.type||'').toLowerCase();
    const rawMode=String(b.mode||'market');
    const mode=({market:'market',limit:'limit',stopmarket:'stopMarket'})[rawMode.toLowerCase()] || rawMode;
    const validity=String(b.validity||'GFD').toUpperCase();
    const limit=b.limit === undefined || b.limit === null || b.limit === '' ? null : Number(b.limit);
    const stop=b.stop === undefined || b.stop === null || b.stop === '' ? null : Number(b.stop);
    const executable=q && side === 'buy' ? q.ask_eur : q && side === 'sell' ? q.bid_eur : null;
    const conservativePrice=mode==='limit' ? Math.max(Number(executable)||0, Number.isFinite(limit) ? limit : 0) : Number(executable);
    const sizing=normalizeOrderSizing(b,conservativePrice);
    const quantity=sizing.valid?sizing.quantity:null;
    const maxCap=Number(process.env.TRADE_REPUBLIC_MAX_ORDER_EUR||0);
    const accountRef=String(b.account_ref||'');
    const account=db.getLatestBrokerAccount(accountRef);
    const position=mapping&&account&&db.getLatestBrokerPosition(accountRef,mapping.isin);
    const availableCash=account&&Number(account.available_cash_eur);
    const fresh=Boolean(q && q.observed_at && Date.now()-Date.parse(q.observed_at) >= 0 && Date.now()-Date.parse(q.observed_at) <= 15000);
    const gates={
        live_enabled:process.env.TRADE_REPUBLIC_LIVE_ENABLED==='true',
        dry_run_disabled:process.env.TRADE_REPUBLIC_DRY_RUN==='false',
        kill_switch_clear:process.env.TRADE_REPUBLIC_KILL_SWITCH!=='true',
        acknowledged:process.env.TRADE_REPUBLIC_LIVE_ACK==='true',
        notional_cap_ok:Number.isFinite(quantity)&&quantity>0&&Number.isFinite(conservativePrice)&&conservativePrice>0&&maxCap>0&&quantity*conservativePrice<=maxCap,
        fresh_quote:fresh,
        executable_quote:Boolean(executable),
        quote_eur:q?.currency==='EUR',
        mapped:Boolean(mapping&&mapping.verified_at&&mapping.currency==='EUR'),
        account_synchronized:Boolean(account&&Number.isFinite(availableCash)),
        account_selected:Boolean(accountRef),
        account_resolvable:typeof client.resolveAccount !== 'function' || Boolean(client.resolveAccount(accountRef)),
        confirmed:[`PLACE ${key}`,'PLACE ORDER'].includes(String(b.confirmation||'').trim()),
        idempotency:/^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$/.test(key),
        valid_side:['buy','sell'].includes(side),
        valid_quantity:Boolean(sizing.valid&&Number.isInteger(quantity)&&quantity>0),
        valid_mode:['market','limit','stopMarket'].includes(mode),
        valid_validity:['GFD','GTD','GTC'].includes(validity),
        valid_limit:mode==='limit' ? Number.isFinite(limit)&&limit>0 : limit===null,
        valid_stop:mode==='stopMarket' ? Number.isFinite(stop)&&stop>0 : stop===null,
        notional_cash_ok:side!=='buy' || (Boolean(account)&&Number.isFinite(availableCash)&&availableCash >= quantity*conservativePrice),
        sufficient_position:side!=='sell' || (Boolean(position)&&Number(position.quantity)>=quantity)
    };
    if(Object.values(gates).some(x=>!x)) {
        if(gates.idempotency) db.createBrokerOrderIntent({broker:'trade_republic',idempotency_key:key,status:'REJECTED',request_json:JSON.stringify({stock_id:Number(b.stock_id)||null,type:side,quantity:Number.isInteger(quantity)?quantity:null,sizing_mode:sizing.sizing_mode||null,requested_amount_eur:sizing.requested_amount_eur||null}),quote_id:q?.id||null});
        return res.status(409).json({error:sizing.valid?'Live order rejected: safety gate not satisfied':sizing.error,gates});
    }
    const order={account_ref_masked:accountRef,exchange:mapping.exchange,instrument:mapping.isin,mode,quantity,type:side,validity};
    if (mode==='limit') order.limit=limit;
    if (mode==='stopMarket') order.stop=stop;
    let authoritativeFee;
    try {
        const preview=await client.previewFees(order);
        authoritativeFee=Number(preview?.fee_eur);
        if (!(Number.isFinite(authoritativeFee)&&authoritativeFee>=0&&String(preview?.currency||'').toUpperCase()==='EUR')) throw new Error('invalid fee preview');
    } catch(e) {
        return res.status(503).json({error:'Authoritative EUR fee preview unavailable; no order was submitted',code:'FEE_PREVIEW_UNAVAILABLE'});
    }
    const finalGates={cap_ok:quantity*conservativePrice+authoritativeFee<=maxCap,sufficient_cash:side!=='buy'||availableCash>=quantity*conservativePrice+authoritativeFee};
    if(Object.values(finalGates).some(x=>!x)) {
        db.createBrokerOrderIntent({broker:'trade_republic',idempotency_key:key,status:'REJECTED',request_json:JSON.stringify({stock_id:Number(b.stock_id),type:side,quantity,sizing_mode:sizing.sizing_mode,requested_amount_eur:sizing.requested_amount_eur}),quote_id:q.id});
        return res.status(409).json({error:'Live order rejected: fee-inclusive safety gate not satisfied',gates:{...gates,...finalGates}});
    }
    db.createBrokerOrderIntent({broker:'trade_republic',idempotency_key:key,status:'PROPOSED',request_json:JSON.stringify({stock_id:Number(b.stock_id),type:side,quantity,sizing_mode:sizing.sizing_mode,requested_amount_eur:sizing.requested_amount_eur,sizing_price_eur:sizing.sizing_price_eur,mode,limit,stop,validity,account_ref_masked:accountRef,fee_eur:authoritativeFee}),quote_id:q.id});
    const intent=db.getBrokerOrderIntent(key);
    try {
        const result=await client.placeOrder(order);
        const terminalRejected=['REJECTED','CANCELLED'].includes(String(result.status||'').toUpperCase());
        const filled = ['FILLED','EXECUTED'].includes(String(result.status || '').toUpperCase());
        let reconciled = null;
        let reconciliationWarning = null;
        const executionDetails = filled ? await authoritativeExecution(result) : null;
        if (executionDetails) {
            try {
                reconciled = db.recordBrokerExecution({ broker:'trade_republic', execution_identity:executionDetails.identity, order_intent_id:intent.id, stock_id:Number(b.stock_id), account_ref_masked:accountRef, side, quantity:executionDetails.quantity, execution_price_eur:executionDetails.price_eur, fees_eur:executionDetails.fees_eur, executed_at:executionDetails.executed_at, currency:'EUR' });
            } catch {
                reconciliationWarning='Confirmed fill could not be reconciled with Journal lots; manual review required';
            }
        }
        const frozenReason=terminalRejected?'Broker reported terminal rejection':result.status==='UNKNOWN'?'Broker response was inconclusive':reconciliationWarning;
        const executionState=filled?'filled':typeof result.execution==='string'?result.execution:'none';
        db.updateBrokerOrderIntent(intent.id,{status:terminalRejected?'REJECTED':filled?'FILLED':result.status==='UNKNOWN'?'UNKNOWN':'SUBMITTED',response_json:JSON.stringify({status:result.status,execution:executionState,reconciled:Boolean(reconciled)}),external_ref_masked:result.external_ref_masked,reconciled_at:reconciled ? new Date().toISOString() : null,frozen_reason:frozenReason});
        if (terminalRejected) return res.status(422).json({error:'Broker rejected or cancelled order',code:'ORDER_REJECTED',idempotency_key:key});
        const warning=reconciliationWarning || (filled&&!reconciled?'Fill lacked authoritative identity, price, fees, quantity, or timestamp; Journal unchanged':result.execution==='filled'?null:'Order ID/status does not prove a fill');
        return res.status(201).json({order:{idempotency_key:key,status:result.status,external_ref_masked:result.external_ref_masked,execution:executionState,reconciled:Boolean(reconciled)},warning});
    } catch(e) {
        if (e.code==='REJECTED' || [400,409,422].includes(e.status)) {
            db.updateBrokerOrderIntent(intent.id,{status:'REJECTED',response_json:JSON.stringify({code:'REJECTED'}),external_ref_masked:null,reconciled_at:null,frozen_reason:'Broker deterministically rejected the order'});
            return res.status(e.status || 422).json({error:'Broker rejected order',code:'ORDER_REJECTED',idempotency_key:key});
        }
        let reconciliation='inconclusive';
        if(e.code==='TIMEOUT'||e.code==='UNAVAILABLE') {
            try { await client.getOrders(); reconciliation='checked_without_authoritative_match'; } catch { reconciliation='reconciliation_unavailable'; }
        }
        db.updateBrokerOrderIntent(intent.id,{status:'UNKNOWN',response_json:JSON.stringify({code:e.code||'BROKER_ERROR'}),external_ref_masked:null,reconciled_at:null,frozen_reason:`Submission ambiguous; ${reconciliation}. Manual reconciliation required.`});
        return res.status(503).json({error:'Order outcome is unknown and is frozen; reconcile before retrying',code:'ORDER_UNKNOWN',idempotency_key:key});
    }
});

// Health check
app.get('/health', (req, res) => {
    res.set({ 'Cache-Control': 'no-store, no-cache, must-revalidate, proxy-revalidate', Pragma: 'no-cache', Expires: '0', 'X-OpenStock-Version': APP_VERSION });
    res.json({ status: 'ok', version: APP_VERSION, timestamp: new Date().toISOString() });
});

// Serve index.html for all other routes (SPA fallback)
app.get('/', (req, res) => {
    res.sendFile(path.join(__dirname, 'www', 'index.html'));
});

app.get('/config', (req, res) => {
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

module.exports = { createApp, serializeStock, serializeTrade, berlinTradingDayStart };
