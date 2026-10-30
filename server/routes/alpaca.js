'use strict';

const { timingSafeEqual } = require('node:crypto');
const { rawDatabase, alpacaJournalData } = require('../alpaca-profile');
const orders = require('../../lib/alpaca-orders');
const levels = require('../../lib/alpaca-alert-levels');

function orderFailure(res, error) {
    const status = error.status || 502;
    if (status >= 500) console.warn(`Alpaca order failed: ${error.message}`);
    return res.status(status).json({ error: status >= 500 ? 'Alpaca order request failed' : error.message });
}

// Live accounts only: the order token must match OPENSTOCK_ORDER_TOKEN. Paper accounts pass through.
function requireLiveToken(req, res, next) {
    const settings = orders.orderSettings();
    if (!settings.live) return next();
    if (!settings.liveAllowed) return res.status(403).json({ error: 'Live trading is off: set ALPACA_ALLOW_LIVE=1 to place orders on the live account' });
    if (!settings.token) return res.status(403).json({ error: 'Live orders need OPENSTOCK_ORDER_TOKEN in .env' });
    const supplied = Buffer.from(String(req.get('X-OpenStock-Order-Token') || ''));
    const expected = Buffer.from(settings.token);
    if (supplied.length !== expected.length || !timingSafeEqual(supplied, expected)) {
        return res.status(401).json({ error: 'Invalid order token' });
    }
    return next();
}

module.exports = function registerAlpacaRoutes({ app }) {
    app.get('/api/alpaca/orders/config', (req, res) => {
        res.json({ orders: orders.publicSettings() });
    });

    app.get('/api/alpaca/orders', (req, res) => {
        res.json({ orders: orders.listOrders(req.app.locals.db, { limit: req.query.limit }) });
    });

    app.post('/api/alpaca/alert-levels/:id/orders', requireLiveToken, async (req, res) => {
        const id = Number(req.params.id);
        if (!Number.isInteger(id) || id < 1) return res.status(400).json({ error: 'Invalid Alpaca alert level' });
        const dryRun = req.body?.preview === true;
        if (!dryRun && req.body?.confirm !== true) return res.status(400).json({ error: 'Confirm the order before it is placed' });
        const amountUsd = req.body?.amount_usd === undefined ? undefined : Number(req.body.amount_usd);
        if (amountUsd !== undefined && !(Number.isFinite(amountUsd) && amountUsd > 0)) {
            return res.status(400).json({ error: 'amount_usd must be a positive number' });
        }
        try {
            const result = await orders.placeLevelOrder({
                db: req.app.locals.db, client: req.app.locals.alpacaClient, levelId: id, dryRun, amountUsd,
            });
            res.status(dryRun ? 200 : 201).json(result);
        } catch (error) {
            orderFailure(res, error);
        }
    });

    app.post('/api/alpaca/orders/:id/cancel', async (req, res) => {
        const id = Number(req.params.id);
        if (!Number.isInteger(id) || id < 1) return res.status(400).json({ error: 'Invalid Alpaca order' });
        try {
            res.json(await orders.cancelOpenOrder({ db: req.app.locals.db, client: req.app.locals.alpacaClient, orderId: id }));
        } catch (error) {
            orderFailure(res, error);
        }
    });

    app.get('/api/alpaca/journal', async (req, res) => {
        try {
            res.json(await alpacaJournalData(req.app));
        } catch (error) {
            console.warn(`Alpaca journal unavailable: ${error.message}`);
            res.status(error.status || 503).json({ error: 'Alpaca journal data is unavailable' });
        }
    });

    app.post('/api/alpaca/watchlist/:id/alert-levels', (req, res) => {
        try {
            res.status(201).json({ alert_level: levels.addPriceLevel(req.app.locals.db, Number(req.params.id), req.body || {}) });
        } catch (error) {
            orderFailure(res, error);
        }
    });

    app.post('/api/alpaca/watchlist/:id/percent-alerts', async (req, res) => {
        const id = Number(req.params.id);
        try {
            const feed = await req.app.locals.alpacaQuoteFeed.get();
            const quote = (feed.prices || []).find(row => Number(row.stock_id) === id);
            res.status(201).json({ alert_level: levels.addPercentLevel(req.app.locals.db, id, {
                direction: req.body?.direction, change_pct: req.body?.change_pct, reference_price_usd: quote?.price_usd,
            }) });
        } catch (error) {
            orderFailure(res, error);
        }
    });

    app.put('/api/alpaca/alert-levels/:id', (req, res) => {
        try {
            res.json({ alert_level: levels.updateLevel(req.app.locals.db, Number(req.params.id), req.body || {}) });
        } catch (error) {
            orderFailure(res, error);
        }
    });

    // Switching real orders on for a live account needs the order token. Paper needs no token, and switching off needs none.
    app.put('/api/alpaca/alert-levels/:id/order', (req, res, next) => (req.body?.auto_order === true ? requireLiveToken(req, res, next) : next()), (req, res) => {
        try {
            res.json({ alert_level: levels.setOrderSettings(rawDatabase(req.app.locals.db), Number(req.params.id), {
                auto_order: req.body?.auto_order, budget_usd: req.body?.budget_usd,
            }) });
        } catch (error) {
            orderFailure(res, error);
        }
    });

    app.delete('/api/alpaca/alert-levels/:id', (req, res) => {
        try {
            res.json(levels.deleteLevel(req.app.locals.db, Number(req.params.id)));
        } catch (error) {
            orderFailure(res, error);
        }
    });

    app.post('/api/alpaca/alert-levels/:id/rearm', (req, res) => {
        const id = Number(req.params.id);
        if (!Number.isInteger(id) || id < 1) return res.status(400).json({ error: 'Invalid Alpaca alert level' });
        const db = rawDatabase(req.app.locals.db);
        const existing = db.prepare('SELECT id FROM alpaca_alert_levels WHERE id=?').get(id);
        if (!existing) return res.status(404).json({ error: 'Alpaca alert level not found' });
        db.prepare(`UPDATE alpaca_alert_levels SET enabled=1, active=1, triggered=0, retired=0,
            triggered_at=NULL, retired_at=NULL, updated_at=datetime('now') WHERE id=?`).run(id);
        res.json({ alert_level: db.prepare('SELECT * FROM alpaca_alert_levels WHERE id=?').get(id) });
    });
};
