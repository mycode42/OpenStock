'use strict';

const { rawDatabase, alpacaJournalData } = require('../alpaca-profile');

module.exports = function registerAlpacaRoutes({ app }) {
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
