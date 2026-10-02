'use strict';

module.exports = function registerDashboardRoutes({ app, dashboardData, captureEquitySnapshot, serializeConfiguredStock, quoteServiceFor, now }) {
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

// Display-only fast prices: no stock, history, alert, or equity writes occur here.
app.get('/api/live-prices', async (req, res) => {
    try { res.json(await req.app.locals.liveQuoteFeed.get()); }
    catch (error) { console.warn(`Live prices unavailable: ${error.message}`); res.status(503).json({ error: 'Live prices are unavailable' }); }
});

// ===== API: Refresh Prices =====
app.post('/api/refresh-prices', async (req, res) => {
    try {
        const stocks = req.app.locals.db.getEnabledStocks();
        if (stocks.length === 0) {
            return res.json({
                message: 'No enabled stocks to refresh',
                updated: 0,
                stocks: req.app.locals.db.getAllStocks().map(serializeConfiguredStock)
            });
        }
        
        let updatedCount = 0;
        const service = quoteServiceFor(req.app, () => now());
        const quotes = await service.fetchMany(stocks.map(stock => ({ ...stock, mapping: req.app.locals.db.getBrokerMapping(stock.id) })), { deadlineAt: Date.now() + Math.max(1000, Number(process.env.QUOTE_FETCH_DEADLINE_MS || 45000)) });
        for (const { stock, quote, error: stockError } of quotes) {
            try {
                if (stockError) throw stockError;
                service.persist(stock, quote);
                updatedCount++;
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
};
