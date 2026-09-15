require('dotenv').config();
const express = require('express');
const path = require('path');
const OpenStockDB = require('./database/db');
const { initDatabase } = require('./database/init');
const { calculateTrade, calculateDashboard, snapshotBucketAt } = require('./lib/analytics');
const { HistoryBackfillQueue } = require('./lib/history-backfill');
const { TradeRepublicClient, sanitizeQuote, sanitizeInstrument, sanitizeAccount, sanitizeCollection, BrokerError } = require('./lib/brokers/trade-republic');

// Helper to convert SQLite datetime string to ISO 8601 UTC
function toISODateTime(sqliteDateTime) {
    if (!sqliteDateTime) return null;
    // Format: YYYY-MM-DD HH:MM:SS -> YYYY-MM-DDTHH:MM:SSZ
    return sqliteDateTime.replace(' ', 'T') + 'Z';
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

function latestYahooChartQuote(chart) {
    const closes = chart?.indicators?.quote?.[0]?.close || [];
    const timestamps = chart?.timestamp || [];
    const meta = chart?.meta || {};
    let latestIndex;
    for (let index = closes.length - 1; index >= 0; index--) {
        if (closes[index] !== null && Number.isFinite(Number(closes[index]))) {
            latestIndex = index;
            break;
        }
    }

    if (latestIndex === undefined) {
        const fallback = Number(meta.regularMarketPrice);
        return Number.isFinite(fallback) && fallback > 0
            ? { priceUsd: fallback, quoteTime: null, source: 'yahoo_regular_market_price' }
            : null;
    }

    const quoteUnixTime = Number(timestamps[latestIndex]);
    const periods = meta.currentTradingPeriod || {};
    const session = ['pre', 'regular', 'post'].find(name => {
        const period = periods[name];
        return period && Number.isFinite(quoteUnixTime) && quoteUnixTime >= period.start && quoteUnixTime <= period.end;
    }) || 'extended';
    const quoteTime = Number.isFinite(quoteUnixTime)
        ? new Date(quoteUnixTime * 1000).toISOString().replace('T', ' ').replace(/\.\d{3}Z$/, '')
        : null;
    return { priceUsd: Number(closes[latestIndex]), quoteTime, source: `yahoo_${session}_close` };
}

function queueStockHistory(historyBackfill, stock) {
    try {
        historyBackfill.enqueue(stock);
    } catch (error) {
        console.error(`Unable to queue history for ${stock?.symbol || 'stock'}:`, error.message);
    }
}

function createApp({ db: suppliedDb, dbPath = process.env.DATABASE_PATH || '/opt/openstock/database/openstock.db', fetchImpl = globalThis.fetch, backfill = null } = {}) {
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
    const serializeConfiguredStock = stock => ({ ...serializeStock(stock), position: db.getPositionSummary(stock.id), alert_levels: db.getAlertLevels(stock.id).map(serializeAlertLevel) });
    app.use(express.json());
    app.use(express.urlencoded({ extended: false }));
    app.use('/api', (req, res, next) => {
    res.set({
        'Cache-Control': 'no-store, no-cache, must-revalidate, proxy-revalidate',
        Pragma: 'no-cache',
        Expires: '0'
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
        '1d': { since: '-1 day', bucketSeconds: 300 },
        '1mo': { since: '-1 month', bucketSeconds: 14400 },
        '1y': { since: '-1 year', bucketSeconds: 86400 },
        '5y': { since: '-5 years', bucketSeconds: 604800 },
        // Keep the former endpoint working for bookmarked API consumers.
        '30d': { since: '-30 days', bucketSeconds: 14400 }
    };
    const range = req.query.range || '30d';
    const window = windows[range];

    if (!window) {
        return res.status(400).json({ error: 'Range must be one of: 1d, 1mo, 1y, 5y' });
    }

    try {
        const stock = req.app.locals.db.getStockById(req.params.id);
        if (!stock) return res.status(404).json({ error: 'Stock not found' });
        const points = req.app.locals.db.getPriceHistory(stock.id, window.since, window.bucketSeconds)
            .map(point => ({ ...point, recorded_at: toISODateTime(point.recorded_at), quote_time: toISODateTime(point.quote_time) }));
        res.json({ stock_id: stock.id, symbol: stock.symbol, range, currency: 'USD', points });
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

app.post('/api/stocks', (req, res) => {
    try {
        const { symbol, name, currency = 'USD', buy_threshold_eur, sell_threshold_eur } = req.body;

        // Validation
        if (!symbol || typeof symbol !== 'string') {
            return res.status(400).json({ error: 'Symbol is required' });
        }
        // Validate stock symbol format (1-5 uppercase letters, common tickers)
        const symbolRegex = /^[A-Z]{1,5}$/;
        if (!symbolRegex.test(symbol.trim())) {
            return res.status(400).json({ error: 'Invalid stock symbol format. Use 1-5 uppercase letters (e.g., AAPL, TSLA, NVDA)' });
        }
        if (!name || typeof name !== 'string') {
            return res.status(400).json({ error: 'Name is required' });
        }
        if (buy_threshold_eur === undefined || sell_threshold_eur === undefined) {
            return res.status(400).json({ error: 'Buy and sell thresholds are required' });
        }

        const buyThreshold = parseFloat(buy_threshold_eur);
        const sellThreshold = parseFloat(sell_threshold_eur);

        if (isNaN(buyThreshold) || isNaN(sellThreshold)) {
            return res.status(400).json({ error: 'Thresholds must be valid numbers' });
        }
        if (buyThreshold <= 0 || sellThreshold <= 0) {
            return res.status(400).json({ error: 'Thresholds must be positive' });
        }
        if (buyThreshold >= sellThreshold) {
            return res.status(400).json({ error: 'Buy threshold must be less than sell threshold' });
        }

        const normalizedSymbol = symbol.trim().toUpperCase();
        const existingStock = req.app.locals.db.getStockBySymbol(normalizedSymbol);
        if (existingStock && !Number(existingStock.archived)) {
            return res.status(409).json({ error: 'Stock symbol already exists' });
        }

        // Disabled and archived stocks do not consume an active monitoring slot.
        const count = req.app.locals.db.getActiveStocks().length;
        if (count >= 25) {
            return res.status(400).json({ error: 'Maximum 25 active stocks allowed' });
        }

        if (existingStock) {
            req.app.locals.db.restoreArchivedStock(existingStock.id, { name: name.trim(), currency, buyThreshold, sellThreshold });
            const stock = req.app.locals.db.getStockById(existingStock.id);
            queueStockHistory(req.app.locals.historyBackfill, stock);
            return res.json({ message: 'Stock restored successfully', restored: true, stock: serializeConfiguredStock(stock) });
        }

        const result = req.app.locals.db.addStock(normalizedSymbol, name.trim(), currency, buyThreshold, sellThreshold);
        const stock = req.app.locals.db.getStockById(result.lastInsertRowid);
        queueStockHistory(req.app.locals.historyBackfill, stock);
        res.status(201).json({ message: 'Stock added successfully', stock: serializeConfiguredStock(stock) });
    } catch (e) {
        if (e.code === 'SQLITE_CONSTRAINT') {
            return res.status(409).json({ error: 'Stock symbol already exists' });
        }
        console.error('Error adding stock:', e.message);
        res.status(500).json({ error: 'Failed to add stock' });
    }
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
        
        // Get exchange rate (USD to EUR)
        let exchangeRate = 1.0; // Default fallback
        try {
            const rateResponse = await fetchImpl('https://api.exchangerate-api.com/v4/latest/USD');
            const rateData = await rateResponse.json();
            exchangeRate = rateData.rates.EUR || 1.0;
        } catch (rateError) {
            console.warn('Could not fetch exchange rate, using default 1.0:', rateError.message);
        }
        
        let updatedCount = 0;
        
        // Process each stock
        for (const stock of stocks) {
            try {
                const selectedSource = stock.price_source || process.env.TRADE_REPUBLIC_PRICE_SOURCE || 'yahoo';
                if (selectedSource === 'trade_republic') {
                    const mapping = req.app.locals.db.getBrokerMapping(stock.id);
                    if (mapping && mapping.verified_at) {
                        try {
                            const brokerQuote = await req.app.locals.brokerClient.getQuote({ isin: mapping.isin, exchange: mapping.exchange });
                            const eurPrice = brokerQuote.bid_eur && brokerQuote.ask_eur ? (brokerQuote.bid_eur + brokerQuote.ask_eur) / 2 : brokerQuote.bid_eur || brokerQuote.ask_eur;
                            if (Number.isFinite(eurPrice) && eurPrice > 0 && brokerQuote.currency === 'EUR') {
                                const usdPrice = eurPrice / exchangeRate;
                                req.app.locals.db.updateStockPrice(stock.id, usdPrice, eurPrice, exchangeRate);
                                req.app.locals.db.addPriceSnapshot(stock.id, usdPrice, eurPrice, exchangeRate, 'trade_republic_order_price', brokerQuote.quote_time);
                                updatedCount++;
                                continue;
                            }
                        } catch (brokerError) {
                            console.warn(`Trade Republic quote unavailable for ${stock.symbol}; falling back to Yahoo:`, brokerError.code || 'UNAVAILABLE');
                        }
                    }
                }
                // Fetch price from Yahoo Finance (free, no API key needed)
                const symbol = stock.symbol;
                const priceResponse = await fetchImpl(
                    `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(symbol)}?range=1d&interval=1m&includePrePost=true&corsDomain=finance.yahoo.com&_=${Date.now()}`,
                    {
                        cache: 'no-store',
                        headers: {
                            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                            'Accept': 'application/json',
                            'Cache-Control': 'no-cache',
                            Pragma: 'no-cache'
                        }
                    }
                );
                
                if (!priceResponse.ok) {
                    console.warn(`Failed to fetch price for ${symbol}: ${priceResponse.status}`);
                    continue;
                }
                
                const priceData = await priceResponse.json();
                const result = priceData.chart?.result?.[0];
                
                if (!result) {
                    console.warn(`No price data for ${symbol}`);
                    continue;
                }
                
                const meta = result.meta;
                const quote = latestYahooChartQuote(result);
                const priceUsd = quote?.priceUsd;
                const shortName = meta.shortName || meta.longName || '';
                
                if (!priceUsd) {
                    console.warn(`Invalid price for ${symbol}: ${priceUsd}`);
                    continue;
                }
                
                // Calculate EUR price
                const priceEur = priceUsd * exchangeRate;
                
                // Update stock in database using class method
                req.app.locals.db.updateStockPrice(stock.id, priceUsd, priceEur, exchangeRate);
                req.app.locals.db.addPriceSnapshot(stock.id, priceUsd, priceEur, exchangeRate, `manual_refresh_${quote.source}`, quote.quoteTime);
                updatedCount++;
                
                // Update name if it was empty and we got a name
                if ((!stock.name || stock.name === '') && shortName) {
                    req.app.locals.db.updateStock(stock.id, { name: shortName });
                }
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
function brokerRows(raw, kind) { return (Array.isArray(raw) ? raw : sanitizeCollection(raw, kind)).map(row => ({ ...row, broker: 'trade_republic', observed_at: row.observed_at || new Date().toISOString(), raw_hash: null })); }
app.get('/api/broker/trade-republic/health', async (req, res) => { try { res.json({ broker: 'trade_republic', ...(await req.app.locals.brokerClient.health()), live_submission_enabled: process.env.TRADE_REPUBLIC_LIVE_ENABLED === 'true' && process.env.TRADE_REPUBLIC_DRY_RUN === 'false' }); } catch (e) { brokerError(res, e); } });
app.get('/api/broker/trade-republic/accounts', async (req, res) => { try { res.json(await req.app.locals.brokerClient.getAccounts()); } catch(e) { brokerError(res,e); } });
app.get('/api/broker/trade-republic/mappings', (req, res) => res.json(req.app.locals.db.getBrokerMappings().map(({ broker_metadata_json, ...m }) => ({ ...m, broker_metadata: JSON.parse(broker_metadata_json || '{}') }))));
app.post('/api/broker/trade-republic/mappings', async (req, res) => { try { const b=req.body||{}; const stock=req.app.locals.db.getStockById(Number(b.stock_id)); if(!stock) return res.status(404).json({error:'Stock not found'}); const isin=String(b.isin||'').toUpperCase(), exchange=String(b.exchange||'').toUpperCase(), currency=String(b.currency||'').toUpperCase(); if(!/^[A-Z0-9]{12}$/.test(isin)||!/^[A-Z0-9]{3,5}$/.test(exchange)||!/^[A-Z]{3}$/.test(currency)) return res.status(400).json({error:'Valid ISIN, exchange, and currency are required'}); const client=req.app.locals.brokerClient; const [instrument, home]=await Promise.all([client.getInstrument(isin), client.getTradeRepublicInstrument(isin)]); if(instrument.isin!==isin||home.isin!==isin||home.exchange!==exchange||home.currency!==currency) return res.status(422).json({error:'Broker identity or home exchange validation failed', verified:false}); req.app.locals.db.saveBrokerMapping({stock_id:stock.id,broker:'trade_republic',ticker:stock.symbol,isin,exchange,currency,broker_metadata_json:JSON.stringify({instrument_name:instrument.name}),source:'broker_verified',verified_at:new Date().toISOString()}); res.status(201).json({mapping:req.app.locals.db.getBrokerMapping(stock.id)}); } catch(e) { brokerError(res,e); } });
app.get('/api/broker/trade-republic/mapping-candidates', async (req, res) => { try { const portfolio=await req.app.locals.brokerClient.getPortfolio(); res.json({candidates:portfolio.filter(x=>x.instrument).map(x=>({ticker:null,isin:x.instrument,quantity:x.quantity,account_ref_masked:x.account_ref_masked,match:'unresolved',reason:'Ticker-to-ISIN mapping requires explicit verification'}))}); } catch(e) { brokerError(res,e); } });
app.get('/api/broker/trade-republic/quote/:stockId', async (req, res) => { try { const mapping=req.app.locals.db.getBrokerMapping(Number(req.params.stockId)); if(!mapping) return res.status(409).json({error:'Stock has no verified broker mapping'}); const quote=await req.app.locals.brokerClient.getQuote({isin:mapping.isin,exchange:mapping.exchange}); const quoteId=req.app.locals.db.addBrokerQuote({broker:'trade_republic',isin:mapping.isin,exchange:mapping.exchange,bid_eur:quote.bid_eur,ask_eur:quote.ask_eur,last_eur:quote.last_eur,currency:quote.currency,quote_time:quote.quote_time,observed_at:quote.observed_at,source:quote.source}).lastInsertRowid; res.json({...quote,quote_id:Number(quoteId),fresh:Date.now()-Date.parse(quote.observed_at)<15000}); } catch(e) { brokerError(res,e); } });
app.post('/api/broker/trade-republic/sync', async (req, res) => { try { const client=req.app.locals.brokerClient, observed_at=new Date().toISOString(); const [accounts,portfolio,orders,transactions]=await Promise.all([client.getAccounts(),client.getPortfolio(),client.getOrders(),client.getTransactions()]); const accountRows=accounts.filter(Boolean).map(a=>({...a,broker:'trade_republic',observed_at,raw_hash:null})); const counts={accounts:req.app.locals.db.syncBrokerRows('broker_accounts',accountRows),portfolio:req.app.locals.db.syncBrokerRows('broker_portfolio',brokerRows(portfolio,'portfolio').filter(x=>x.quantity!==null)),orders:req.app.locals.db.syncBrokerRows('broker_orders',brokerRows(orders,'orders')),transactions:req.app.locals.db.syncBrokerRows('broker_transactions',brokerRows(transactions,'transactions'))}; res.json({broker:'trade_republic',observed_at,counts}); } catch(e) { brokerError(res,e); } });
app.post('/api/broker/trade-republic/order-preview', async (req,res) => { try { const b=req.body||{}, mapping=req.app.locals.db.getBrokerMapping(Number(b.stock_id)); if(!mapping) return res.status(409).json({error:'Verified mapping required'}); if(!['buy','sell'].includes(b.type)||!Number.isInteger(Number(b.quantity))||Number(b.quantity)<=0) return res.status(400).json({error:'Valid side and integer quantity required'}); const quote=await req.app.locals.brokerClient.getQuote({isin:mapping.isin,exchange:mapping.exchange}), side=b.type==='buy'?'ask_eur':'bid_eur'; if(!quote[side]) return res.status(502).json({error:'Fresh side quote unavailable'}); const fee=await req.app.locals.brokerClient.previewFees({account_ref_masked:b.account_ref,exchange:mapping.exchange,instrument:mapping.isin,mode:b.mode||'market',quantity:Number(b.quantity),type:b.type,limit:b.limit}); res.json({broker:'trade_republic',instrument:mapping.isin,exchange:mapping.exchange,side:b.type,quantity:Number(b.quantity),reference_price_eur:quote[side],fee_eur:fee.fee_eur,total_eur:null,quote}); } catch(e) { brokerError(res,e); } });
app.post('/api/broker/trade-republic/orders', async (req,res) => {
    const b=req.body||{}, db=req.app.locals.db, client=req.app.locals.brokerClient;
    const mapping=db.getBrokerMapping(Number(b.stock_id));
    const q=mapping&&db.getLatestBrokerQuote(mapping.isin,mapping.exchange);
    const key=String(b.idempotency_key||'');
    const quantity=Number(b.quantity), side=String(b.type||'').toLowerCase();
    const mode=String(b.mode||'market').toLowerCase(), validity=String(b.validity||'GFD').toUpperCase();
    const limit=b.limit === undefined || b.limit === null || b.limit === '' ? null : Number(b.limit);
    const executable=q && side === 'buy' ? q.ask_eur : q && side === 'sell' ? q.bid_eur : null;
    const conservativePrice=side === 'buy' ? Math.max(Number(executable)||0, Number.isFinite(limit) ? limit : 0) : Number(executable);
    const maxCap=Number(process.env.TRADE_REPUBLIC_MAX_ORDER_EUR||0);
    const accountRef=String(b.account_ref||'');
    const account=db.getLatestBrokerAccount(accountRef);
    const position=mapping&&account&&db.getLatestBrokerPosition(accountRef,mapping.isin);
    const availableCash=account&&Number(account.available_cash_eur);
    const feePreview=Number.isFinite(Number(b.fee_eur))&&Number(b.fee_eur)>=0 ? Number(b.fee_eur) : 0;
    const fresh=Boolean(q && q.observed_at && Date.now()-Date.parse(q.observed_at) >= 0 && Date.now()-Date.parse(q.observed_at) <= 15000);
    const gates={
        live_enabled:process.env.TRADE_REPUBLIC_LIVE_ENABLED==='true',
        dry_run_disabled:process.env.TRADE_REPUBLIC_DRY_RUN==='false',
        kill_switch_clear:process.env.TRADE_REPUBLIC_KILL_SWITCH!=='true',
        acknowledged:process.env.TRADE_REPUBLIC_LIVE_ACK==='true',
        cap_ok:Number.isFinite(quantity)&&quantity>0&&Number.isFinite(conservativePrice)&&conservativePrice>0&&maxCap>0&&quantity*conservativePrice<=maxCap,
        fresh_quote:fresh,
        executable_quote:Boolean(executable),
        mapped:Boolean(mapping&&mapping.verified_at),
        account_synchronized:Boolean(account&&Number.isFinite(availableCash)),
        account_selected:Boolean(accountRef),
        account_resolvable:typeof client.resolveAccount !== 'function' || Boolean(client.resolveAccount(accountRef)),
        confirmed:b.confirmation===`PLACE ${key}`,
        idempotency:/^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$/.test(key),
        valid_side:['buy','sell'].includes(side),
        valid_quantity:Number.isInteger(quantity)&&quantity>0,
        valid_mode:['market','limit'].includes(mode),
        valid_validity:['GFD','GTC'].includes(validity),
        valid_limit:mode==='market' ? limit===null : Number.isFinite(limit)&&limit>0,
        sufficient_cash:side!=='buy' || (Boolean(account)&&Number.isFinite(availableCash)&&availableCash >= quantity*conservativePrice+feePreview),
        sufficient_position:side!=='sell' || (Boolean(position)&&Number(position.quantity)>=quantity)
    };
    const existing=key ? db.getBrokerOrderIntent(key) : null;
    if(existing) return res.status(existing.status==='REJECTED'?409:200).json({order:{idempotency_key:key,status:existing.status,external_ref_masked:existing.external_ref_masked||null},duplicate:true});
    if(Object.values(gates).some(x=>!x)) {
        if(gates.idempotency) db.createBrokerOrderIntent({broker:'trade_republic',idempotency_key:key,status:'REJECTED',request_json:JSON.stringify({stock_id:Number(b.stock_id)||null,type:side,quantity:Number.isFinite(quantity)?quantity:null}),quote_id:q?.id||null});
        return res.status(409).json({error:'Live order rejected: safety gate not satisfied',gates});
    }
    db.createBrokerOrderIntent({broker:'trade_republic',idempotency_key:key,status:'PROPOSED',request_json:JSON.stringify({stock_id:Number(b.stock_id),type:side,quantity,mode,limit,validity,account_ref_masked:accountRef}),quote_id:q.id});
    const intent=db.getBrokerOrderIntent(key);
    const order={account_ref_masked:accountRef,exchange:mapping.exchange,instrument:mapping.isin,mode,quantity,type:side,validity};
    if (mode==='limit') order.limit=limit;
    try {
        const result=await client.placeOrder(order);
        const terminalRejected=['REJECTED','CANCELLED'].includes(String(result.status||'').toUpperCase());
        db.updateBrokerOrderIntent(intent.id,{status:terminalRejected?'REJECTED':['FILLED','EXECUTED'].includes(result.status)?'FILLED':result.status==='UNKNOWN'?'UNKNOWN':'SUBMITTED',response_json:JSON.stringify({status:result.status,execution:result.execution}),external_ref_masked:result.external_ref_masked,reconciled_at:null,frozen_reason:terminalRejected?'Broker reported terminal rejection':result.status==='UNKNOWN'?'Broker response was inconclusive':null});
        if (terminalRejected) return res.status(422).json({error:'Broker rejected or cancelled order',code:'ORDER_REJECTED',idempotency_key:key});
        return res.status(201).json({order:{idempotency_key:key,status:result.status,external_ref_masked:result.external_ref_masked,execution:result.execution},warning:result.execution==='filled'?null:'Order ID/status does not prove a fill'});
    } catch(e) {
        if (e.code==='REJECTED' || [400,409,422].includes(e.status)) {
            db.updateBrokerOrderIntent(intent.id,{status:'REJECTED',response_json:JSON.stringify({code:'REJECTED'}),external_ref_masked:null,reconciled_at:new Date().toISOString(),frozen_reason:'Broker deterministically rejected the order'});
            return res.status(e.status || 422).json({error:'Broker rejected order',code:'ORDER_REJECTED',idempotency_key:key});
        }
        let reconciliation='inconclusive';
        if(e.code==='TIMEOUT'||e.code==='UNAVAILABLE') {
            try { await client.getOrders(); reconciliation='checked_without_authoritative_match'; } catch { reconciliation='reconciliation_unavailable'; }
        }
        db.updateBrokerOrderIntent(intent.id,{status:'UNKNOWN',response_json:JSON.stringify({code:e.code||'BROKER_ERROR'}),external_ref_masked:null,reconciled_at:new Date().toISOString(),frozen_reason:`Submission ambiguous; ${reconciliation}. Manual reconciliation required.`});
        return res.status(503).json({error:'Order outcome is unknown and is frozen; reconcile before retrying',code:'ORDER_UNKNOWN',idempotency_key:key});
    }
});

// Health check
app.get('/health', (req, res) => {
    res.set({ 'Cache-Control': 'no-store, no-cache, must-revalidate, proxy-revalidate', Pragma: 'no-cache', Expires: '0' });
    res.json({ status: 'ok', timestamp: new Date().toISOString() });
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

module.exports = { createApp, serializeStock, serializeTrade, latestYahooChartQuote };
