'use strict';

module.exports = function registerStocksRoutes({ app, serializeConfiguredStock, serializeAlertLevel, alertLevelInput, numberField, booleanField, queueStockHistory, resolveVerifiedInstrument, resolveCryptoCandidates, resolveVerifiedCryptoInstrument, brokerError, now, berlinTradingDayStart, berlinTradingDayEnd, evenlySample, toISODateTime }) {
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

    try {
        const stock = req.app.locals.db.getStockById(req.params.id);
        if (!stock) return res.status(404).json({ error: 'Stock not found' });
        const mapping = req.app.locals.db.getBrokerMapping(stock.id);
        const nativeTradeRepublicEur = Boolean(mapping?.verified_at && mapping.currency === 'EUR');
        const tradeRepublicSources = ['trade_republic_history', 'trade_republic_order_price', 'trade_republic_sdk_order_price', 'manual_refresh_trade_republic_order_price'];
        const tradingDayStart = range === '1d' ? berlinTradingDayStart(now()) : null;
        const tradingDayEnd = range === '1d' ? berlinTradingDayEnd(now()) : null;
        const eligiblePoints = req.app.locals.db.getPriceHistory(stock.id, window.since, window.queryBucketSeconds || window.bucketSeconds, {
            sources: tradeRepublicSources,
            eurOnly: true
        }).map(point => ({ ...point, recorded_at: toISODateTime(point.recorded_at), quote_time: toISODateTime(point.quote_time) }))
            .filter(point => !tradingDayStart || (Date.parse(point.recorded_at) >= tradingDayStart.getTime() && Date.parse(point.recorded_at) <= tradingDayEnd.getTime()));
        const points = evenlySample(eligiblePoints, window.maxPoints);
        res.json({ stock_id: stock.id, symbol: stock.symbol, isin: stock.isin || null, range, currency: 'EUR', native_eur: nativeTradeRepublicEur, source: 'trade_republic', bucket_seconds: window.bucketSeconds, max_gap_seconds: window.maxLiveGapSeconds, start_at: tradingDayStart?.toISOString() || null, end_at: tradingDayEnd?.toISOString() || null, points });
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

app.post('/api/instruments/resolve', async (req, res) => {
    try {
        if (String(req.body?.asset_type || 'stock').toLowerCase() === 'crypto') {
            const candidates = await resolveCryptoCandidates(req.app.locals.sdkClient, req.body?.query);
            return res.json({ asset_type: 'crypto', candidates });
        }
        const result = await resolveVerifiedInstrument(req.app.locals.brokerClient, req.body?.isin);
        res.json({ verified: true, ...result });
    } catch (e) { if (e.status) return res.status(e.status).json({ error: e.message, verified: false }); brokerError(res, e); }
});

app.post('/api/stocks', async (req, res) => {
    try {
        const assetType = String(req.body?.asset_type || 'stock').toLowerCase();
        if (assetType === 'crypto') {
            const instrumentId = String(req.body?.instrument_id || '').trim().toUpperCase();
            const exchange = String(req.body?.exchange || '').trim().toUpperCase();
            const requestedSymbol = String(req.body?.symbol || '').trim().toUpperCase();
            const buyThreshold = Number(req.body?.buy_threshold_eur), sellThreshold = Number(req.body?.sell_threshold_eur);
            if (!/^[A-Z0-9.\-]{1,12}$/.test(requestedSymbol)) return res.status(400).json({ error: 'A valid crypto symbol is required' });
            if (!(buyThreshold > 0) || !(sellThreshold > 0) || buyThreshold >= sellThreshold) return res.status(400).json({ error: 'Buy and sell thresholds must be positive and BUY must be below SELL' });
            const { instrument, home, asset, destination } = await resolveVerifiedCryptoInstrument(req.app.locals.sdkClient, { instrumentId, exchange });
            const symbol = String(instrument.ticker || '').trim().toUpperCase();
            if (!/^[A-Z0-9.\-]{1,12}$/.test(symbol)) return res.status(422).json({ error: 'Trade Republic did not return a usable crypto ticker' });
            const db = req.app.locals.db;
            const existing = db.getStockByBrokerInstrument(instrumentId);
            const existingMapping = existing && db.getBrokerMapping(existing.id);
            if (existingMapping?.verified_at) return res.status(409).json({ error: existing.archived ? 'Crypto instrument is already configured; restore it explicitly' : 'Crypto instrument is already configured', stock_id: existing.id });
            if ((!existing || !Number(existing.enabled)) && db.getActiveStocks().length >= 25) return res.status(400).json({ error: 'Maximum 25 active monitored names allowed' });
            if (!existing && db.getStockBySymbol(symbol)) return res.status(409).json({ error: 'Derived symbol already exists' });
            const mapping = { broker: 'trade_republic', ticker: symbol, isin: instrumentId, exchange: home.exchange, currency: 'EUR', broker_metadata_json: JSON.stringify({ instrument, asset, destination, asset_type: 'crypto' }), source: 'sdk_crypto_verified', verified_at: new Date().toISOString() };
            let stock;
            if (existing) {
                stock = db.configureJournalStockWithMapping({ stockId: existing.id, symbol, name: instrument.name, isin: instrumentId, currency: 'EUR', assetType: 'crypto', buyThreshold, sellThreshold, mapping });
            } else {
                const result = db.addStockWithMapping({ symbol, name: instrument.name, isin: instrumentId, currency: 'EUR', buyThreshold, sellThreshold, assetType: 'crypto', mapping });
                stock = db.getStockById(result.lastInsertRowid);
            }
            return res.status(existing ? 200 : 201).json({ message: 'Crypto instrument added from verified Trade Republic data', stock: serializeConfiguredStock(stock), mapping: db.getBrokerMapping(stock.id) });
        }
        if (assetType !== 'stock') return res.status(400).json({ error: 'Instrument type must be stock or crypto' });
        const isin = String(req.body?.isin || '').trim().toUpperCase();
        const buyThreshold = Number(req.body?.buy_threshold_eur), sellThreshold = Number(req.body?.sell_threshold_eur);
        if (!/^[A-Z0-9]{12}$/.test(isin)) return res.status(400).json({ error: 'A valid 12-character ISIN is required' });
        if (!(buyThreshold > 0) || !(sellThreshold > 0) || buyThreshold >= sellThreshold) return res.status(400).json({ error: 'Buy and sell thresholds must be positive and BUY must be below SELL' });
        const { instrument, home } = await resolveVerifiedInstrument(req.app.locals.brokerClient, isin);
        const exchange = home.exchange, currency = home.currency;
        const db = req.app.locals.db;
        const existing = db.getStockByBrokerInstrument(isin);
        const existingMapping = existing && db.getBrokerMapping(existing.id);
        if (existingMapping?.verified_at) return res.status(409).json({ error: existing.archived ? 'Stock instrument is already configured; restore it explicitly' : 'Stock instrument is already configured', stock_id: existing.id });
        if ((!existing || !Number(existing.enabled)) && db.getActiveStocks().length >= 25) return res.status(400).json({ error: 'Maximum 25 active monitored names allowed' });
        const symbol = instrument.ticker;
        const existingSymbol = !existing && db.getStockBySymbol(symbol);
        if (existingSymbol) return res.status(409).json({ error: existingSymbol.archived ? 'Derived symbol belongs to an archived stock; restore that identity explicitly' : 'Derived symbol already exists' });
        const mapping = { broker: 'trade_republic', ticker: symbol, isin, exchange, currency, broker_metadata_json: JSON.stringify({ instrument, home }), source: 'broker_verified', verified_at: new Date().toISOString() };
        let stock;
        if (existing) {
            stock = db.configureJournalStockWithMapping({ stockId: existing.id, symbol, name: instrument.name, isin, currency, assetType: 'stock', buyThreshold, sellThreshold, mapping });
        } else {
            const result = db.addStockWithMapping({ symbol, name: instrument.name, isin, currency, buyThreshold, sellThreshold, mapping });
            stock = db.getStockById(result.lastInsertRowid);
        }
        queueStockHistory(req.app.locals.historyBackfill, stock);
        res.status(existing ? 200 : 201).json({ message: 'Stock added from verified Trade Republic instrument', stock: serializeConfiguredStock(stock), mapping: db.getBrokerMapping(stock.id) });
    } catch (e) { if (e.status) return res.status(e.status).json({ error: e.message }); if (e.code === 'SQLITE_CONSTRAINT') return res.status(409).json({ error: 'Stock ISIN or derived symbol already exists' }); console.error('Error adding stock:', e.message); res.status(500).json({ error: 'Failed to add stock' }); }
});

app.put('/api/stocks/:id', (req, res) => {
    try {
        const { buy_threshold_eur, sell_threshold_eur, owned, enabled, name } = req.body;
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
            if (newEnabled && !req.app.locals.db.getBrokerMapping(stock.id)?.verified_at) {
                return res.status(409).json({ error: 'Add this instrument from Trade Republic before enabling monitoring' });
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
        if (!req.app.locals.db.getBrokerMapping(existing.id)?.verified_at) return res.status(409).json({ error: 'Add this instrument from Trade Republic to restore monitoring and Journal access' });
        if (req.app.locals.db.getActiveStocks().length >= 25) return res.status(400).json({ error: 'Maximum 25 active stocks allowed' });
        const name = String(req.body.name ?? existing.name ?? '').trim();
        const currency = String(req.body.currency ?? existing.currency ?? 'USD').trim().toUpperCase();
        if (req.body.buy_threshold_eur !== undefined || req.body.sell_threshold_eur !== undefined) return res.status(400).json({ error: 'Restore does not change alert levels; manage them separately after restoring.' });
        if (!name) return res.status(400).json({ error: 'Name is required' });
        if (!/^[A-Z]{3}$/.test(currency)) return res.status(400).json({ error: 'Currency must be a three-letter code' });
        req.app.locals.db.restoreArchivedStock(existing.id, { name, currency });
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
};
