'use strict';

const { timingSafeEqual } = require('node:crypto');
const { rawDatabase, alpacaJournalData } = require('../alpaca-profile');
const orders = require('../../lib/alpaca-orders');

// Order routes need ORDERS_ENABLED=true and a matching OPENSTOCK_ORDER_TOKEN header.
function requireOrderToken(req, res, next) {
    const settings = orders.orderSettings();
    if (!settings.enabled) return res.status(403).json({ error: 'Order routing is disabled (set ORDERS_ENABLED=true)' });
    if (!settings.token) return res.status(403).json({ error: 'OPENSTOCK_ORDER_TOKEN is not set' });
    const supplied = Buffer.from(String(req.get('X-OpenStock-Order-Token') || ''));
    const expected = Buffer.from(settings.token);
    if (supplied.length !== expected.length || !timingSafeEqual(supplied, expected)) {
        return res.status(401).json({ error: 'Invalid order token' });
    }
    next();
}

function orderFailure(res, error) {
    const status = error.status || 502;
    if (status >= 500) console.warn(`Alpaca order failed: ${error.message}`);
    return res.status(status).json({ error: status >= 500 ? 'Alpaca order request failed' : error.message });
}

module.exports = function registerAlpacaRoutes({ app }) {
    app.get('/api/alpaca/orders/config', (req, res) => {
        res.json({ orders: orders.publicSettings() });
    });

    app.get('/api/alpaca/orders', (req, res) => {
        res.json({ orders: orders.listOrders(req.app.locals.db, { limit: req.query.limit }) });
    });

    app.post('/api/alpaca/alert-levels/:id/orders', requireOrderToken, async (req, res) => {
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

    app.post('/api/alpaca/orders/:id/cancel', requireOrderToken, async (req, res) => {
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
