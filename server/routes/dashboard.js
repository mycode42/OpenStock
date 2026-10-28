'use strict';

const { alpacaDashboardData } = require('../alpaca-profile');

module.exports = function registerDashboardRoutes({ app, alpacaQuoteFeed, now }) {
// ===== API: Dashboard data =====
app.get('/api/dashboard', async (req, res) => {
    try {
        if (req.app.locals.demoMode) {
            return res.json({ stocks: req.app.locals.demoData.dashboardStocks(now()), analytics: { demo_mode: true, broker: 'alpaca', currency: 'USD' } });
        }
        res.json(await alpacaDashboardData(req.app));
    } catch (e) {
        console.error('Error fetching dashboard:', e.message);
        res.status(500).json({ error: 'Failed to fetch dashboard data' });
    }
});

app.get('/api/demo-status', (req, res) => {
    res.json(req.app.locals.demoStatus || { demo_mode: false, setup: [] });
});

// Display-only fast prices: no stock, history, alert, or equity writes occur here.
app.get('/api/live-prices', async (req, res) => {
    if (req.app.locals.demoMode) return res.json(req.app.locals.demoData.livePrices(now()));
    try {
        res.json(await alpacaQuoteFeed.get({ force: req.query.refresh === '1' }));
    }
    catch (error) { console.warn(`Live prices unavailable: ${error.message}`); res.status(503).json({ error: 'Live prices are unavailable' }); }
});

// ===== API: Refresh Prices =====
app.post('/api/refresh-prices', async (req, res) => {
    if (req.app.locals.demoMode) return res.status(409).json({ error: 'Demo mode shows simulated quotes. Complete setup to refresh live prices.' });
    try {
        const result = await alpacaQuoteFeed.get({ force: true });
        res.json({ message: 'Alpaca quotes refreshed', updated: result.prices.length, ...result });
    } catch (e) {
        console.error('Error refreshing prices:', e.message);
        res.status(500).json({ error: 'Failed to refresh prices' });
    }
});
};
