'use strict';

const { rawDatabase } = require('../alpaca-profile');

module.exports = function registerStocksRoutes({ app, serializeConfiguredStock, serializeAlertLevel, alertLevelInput, numberField, booleanField, now, berlinTradingDayStart, berlinTradingDayEnd, evenlySample, toISODateTime }) {
// /api/stocks speaks USD: stored *_eur field names are returned as *_usd. Values are unchanged.
function usdShape(value) {
    if (Array.isArray(value)) return value.map(usdShape);
    if (value && typeof value === 'object' && !(value instanceof Date)) {
        return Object.fromEntries(Object.entries(value).map(([key, item]) => [key.replace(/_eur$/, '_usd'), usdShape(item)]));
    }
    return value;
}

app.use('/api/stocks', (req, res, next) => {
    const json = res.json.bind(res);
    res.json = body => json(usdShape(body));
    next();
});

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
        '1w': { since: '-7 days', bucketSeconds: 3600, maxPoints: 80, maxLiveGapSeconds: 21600 },
        '1mo': { since: '-1 month', bucketSeconds: 28800, maxPoints: 72, maxLiveGapSeconds: 43200 },
        '1y': { since: '-1 year', bucketSeconds: 345600, maxPoints: 96, maxLiveGapSeconds: 172800 },
        '5y': { since: '-5 years', bucketSeconds: 1814400, maxPoints: 96, maxLiveGapSeconds: 1209600 },
        // Keep the former endpoint working for bookmarked API consumers.
        '30d': { since: '-30 days', bucketSeconds: 28800, maxPoints: 72, maxLiveGapSeconds: 43200 }
    };
    const range = req.query.range || '30d';
    const window = windows[range];

    if (!window) {
        return res.status(400).json({ error: 'Range must be one of: 1d, 1w, 1mo, 1y, 5y' });
    }

    if (req.app.locals.demoMode) {
        const history = req.app.locals.demoData.stockHistory(req.params.id, range, now());
        if (!history) return res.status(404).json({ error: 'Stock not found' });
        return res.json(history);
    }

    (async () => {
        const stock = rawDatabase(req.app.locals.db).prepare(`SELECT id, symbol FROM alpaca_watchlist
            WHERE id=? AND enabled=1 AND archived=0`).get(req.params.id);
        if (!stock) return res.status(404).json({ error: 'Alpaca watchlist stock not found' });
        const durations = { '1d': 5, '1w': 7, '1mo': 31, '30d': 30, '1y': 366, '5y': 1827 };
        const timeframes = { '1d': '5Min', '1w': '30Min', '1mo': '1Day', '30d': '1Day', '1y': '1Day', '5y': '1Week' };
        const end = now();
        const start = new Date(end.getTime() - durations[range] * 86400000);
        const result = await req.app.locals.alpacaClient.getBars(stock.symbol, {
            timeframe: timeframes[range], start, end, limit: range === '5y' ? 500 : 5000
        });
        const maximum = { '1d': 72, '1w': 84, '1mo': 90, '30d': 90, '1y': 120, '5y': 120 }[range];
        let bars = (result.bars || []).map(bar => ({
            recorded_at: bar.t,
            price_usd: Number(bar.c),
            volume: Number(bar.v || 0)
        })).filter(point => Number.isFinite(point.price_usd) && point.price_usd > 0)
            .sort((left, right) => Date.parse(left.recorded_at) - Date.parse(right.recorded_at));
        if (range === '1d' && bars.length) {
            const nyDate = value => {
                const parts = new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(new Date(value));
                const date = Object.fromEntries(parts.filter(part => part.type !== 'literal').map(part => [part.type, part.value]));
                return `${date.year}-${date.month}-${date.day}`;
            };
            const latestSession = nyDate(bars[bars.length - 1].recorded_at);
            bars = bars.filter(point => nyDate(point.recorded_at) === latestSession);
        }
        const points = evenlySample(bars, maximum);
        return res.json({
            stock_id: stock.id, symbol: stock.symbol, range, currency: 'USD', source: `alpaca_${result.feed}_bars`,
            bucket_seconds: null, max_gap_seconds: null, start_at: null, end_at: null, points
        });
    })().catch(error => {
        console.warn(`Alpaca price history unavailable: ${error.message}`);
        res.status(error.status || 503).json({ error: 'Alpaca price history is unavailable' });
    });
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

app.post('/api/stocks/:id/percent-alerts', (req, res) => {
    try {
        const stock = req.app.locals.db.getStockById(req.params.id);
        if (!stock) return res.status(404).json({ error: 'Stock not found' });
        const direction = String(req.body.direction || '').toUpperCase();
        if (!['DROP', 'GAIN'].includes(direction)) return res.status(400).json({ error: 'Direction must be DROP or GAIN' });
        const changePct = numberField(req.body.change_pct, 'Percentage move', { minimum: Number.EPSILON });
        const level = req.app.locals.db.addPercentAlertLevel(stock.id, direction, changePct);
        res.status(201).json({ alert_level: serializeAlertLevel(level) });
    } catch (e) { res.status(e.code === 'SQLITE_CONSTRAINT_UNIQUE' ? 409 : 400).json({ error: e.message || 'Invalid percentage alert' }); }
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

app.post('/api/stocks', async (req, res) => {
    try {
        const symbol = String(req.body?.symbol || '').trim().toUpperCase();
        const name = String(req.body?.name || '').trim();
        const buyThreshold = Number(req.body?.buy_threshold_usd), sellThreshold = Number(req.body?.sell_threshold_usd);
        if (!/^[A-Z][A-Z0-9.\-]{0,9}$/.test(symbol)) return res.status(400).json({ error: 'A valid US equity symbol is required' });
        if (!(buyThreshold > 0) || !(sellThreshold > 0) || buyThreshold >= sellThreshold) return res.status(400).json({ error: 'Buy and sell thresholds must be positive and BUY must be below SELL' });
        const db = req.app.locals.db;
        const existing = db.getStockBySymbol(symbol);
        if (existing) return res.status(409).json({ error: existing.archived ? 'Symbol belongs to an archived stock; restore it explicitly' : 'Stock already exists', stock_id: existing.id });
        const asset = await req.app.locals.alpacaClient.getAsset(symbol);
        if (asset?.class !== 'us_equity' || asset?.tradable === false || asset?.status === 'inactive') {
            return res.status(422).json({ error: 'Alpaca does not list this symbol as a tradable US equity' });
        }
        const result = db.addStock(symbol, name || asset.name || symbol, 'USD', buyThreshold, sellThreshold);
        const stock = db.getStockById(result.lastInsertRowid);
        res.status(201).json({ message: 'Stock added from the Alpaca asset list', stock: serializeConfiguredStock(stock) });
    } catch (e) {
        if (e.status === 404) return res.status(422).json({ error: 'Alpaca does not list this symbol as a tradable US equity' });
        if (e.status) return res.status(e.status).json({ error: e.message });
        if (e.code === 'SQLITE_CONSTRAINT') return res.status(409).json({ error: 'Stock symbol already exists' });
        console.error('Error adding stock:', e.message);
        res.status(500).json({ error: 'Failed to add stock' });
    }
});

app.put('/api/stocks/:id', (req, res) => {
    try {
        const { buy_threshold_usd, sell_threshold_usd, owned, enabled, name } = req.body;
        if (buy_threshold_usd !== undefined || sell_threshold_usd !== undefined) {
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
            updates.enabled = newEnabled ? 1 : 0;
        }

        if (name !== undefined) {
            if (!name || typeof name !== 'string' || name.trim() === '') {
                return res.status(400).json({ error: 'Invalid name' });
            }
            updates.name = name.trim();
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
        const name = String(req.body.name ?? existing.name ?? '').trim();
        const currency = String(req.body.currency ?? existing.currency ?? 'USD').trim().toUpperCase();
        if (req.body.buy_threshold_usd !== undefined || req.body.sell_threshold_usd !== undefined) return res.status(400).json({ error: 'Restore does not change alert levels; manage them separately after restoring.' });
        if (!name) return res.status(400).json({ error: 'Name is required' });
        if (!/^[A-Z]{3}$/.test(currency)) return res.status(400).json({ error: 'Currency must be a three-letter code' });
        req.app.locals.db.restoreArchivedStock(existing.id, { name, currency });
        const stock = req.app.locals.db.getStockById(existing.id);
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
};
