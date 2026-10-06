'use strict';

module.exports = function registerJournalRoutes({ app, serializeTrade, normaliseTrade, numberField, toDatabaseDate, dashboardData, captureEquitySnapshot, toISODateTime }) {
// ===== API: Trades, capital events and performance =====
app.get('/api/trades', (req, res) => {
    try {
        const status = ['open', 'closed'].includes(req.query.status) ? req.query.status : undefined;
        const symbol = req.query.symbol ? String(req.query.symbol).toUpperCase() : undefined;
        const requestedPagination = req.query.page !== undefined || req.query.page_size !== undefined;
        if (!requestedPagination) return res.json(req.app.locals.db.getAllTrades({ status, symbol }).map(serializeTrade));
        const page = Number(req.query.page ?? 1);
        const pageSize = Number(req.query.page_size ?? 20);
        if (!Number.isInteger(page) || page < 1 || !Number.isInteger(pageSize) || pageSize < 1 || pageSize > 100) {
            return res.status(400).json({ error: 'page must be a positive integer and page_size must be between 1 and 100' });
        }
        const result = req.app.locals.db.getTradesPage({ status, symbol }, { page, pageSize });
        res.json({ ...result, items: result.items.map(serializeTrade) });
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
        if (existing.broker_confirmed) return res.status(409).json({ error: 'Broker-confirmed trades are read-only' });
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
        if (existing.broker_confirmed) return res.status(409).json({ error: 'Broker-confirmed trades are read-only' });
        if (existing.closed_at) return res.status(409).json({ error: 'Trade is already closed; use explicit edit to correct it' });
        const closeQuantity = numberField(req.body.close_quantity ?? existing.quantity, 'Sale quantity', { minimum: Number.EPSILON });
        const trade = normaliseTrade({ ...req.body, quantity: existing.quantity, closed_at: req.body.closed_at || new Date().toISOString() }, existing);
        const result = req.app.locals.db.closeTradeQuantity(existing.id, trade, closeQuantity);
        captureEquitySnapshot(req.app.locals.db, 'trade_close');
        const closedTrade = result.partial
            ? req.app.locals.db.getAllTrades({ status: 'closed' }).find(row => row.trade_id === result.closedTradeId)
            : req.app.locals.db.getTradeById(existing.id);
        res.json({ message: result.partial ? 'Partial sale recorded' : 'Trade closed', partial: result.partial, trade: serializeTrade(closedTrade), open_trade: result.partial ? serializeTrade(req.app.locals.db.getTradeById(existing.id)) : null });
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
        if (event_type === 'INITIAL' && req.app.locals.db.getCapitalEvents().some(item => item.event_type === 'INITIAL')) {
            const error = new Error('Initial capital is already recorded. Use a deposit to add funds or a withdrawal to reduce funds; neither changes trading P/L.');
            error.code = 'INITIAL_CAPITAL_EXISTS';
            throw error;
        }
        const result = req.app.locals.db.addCapitalEvent(event);
        captureEquitySnapshot(req.app.locals.db, 'capital_event');
        const saved = req.app.locals.db.getCapitalEvents().find(item => item.id === Number(result.lastInsertRowid));
        res.status(201).json({ message: 'Capital event recorded', event: { ...saved, occurred_at: toISODateTime(saved.occurred_at), created_at: toISODateTime(saved.created_at) } });
    } catch (e) {
        res.status(e.code === 'INITIAL_CAPITAL_EXISTS' ? 409 : 400).json({ error: e.message || 'Invalid capital event', code: e.code });
    }
});

app.get('/api/analytics/dashboard', (req, res) => {
    try {
        res.json(dashboardData(req.app.locals.db));
    } catch (e) {
        console.error('Error calculating dashboard:', e.message);
        res.status(500).json({ error: 'Failed to calculate dashboard' });
    }
});
};
