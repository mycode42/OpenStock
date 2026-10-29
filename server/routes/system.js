const { createSystemLoad } = require('../system-load');

module.exports = function registerSystemRoutes({ app, root }) {
    const snapshot = createSystemLoad({ root });
    app.get('/api/system/load', async (_req, res) => {
        try {
            res.json(await snapshot());
        } catch (error) {
            console.error(`System load is unavailable: ${error.message}`);
            res.status(503).json({ error: 'System load is unavailable' });
        }
    });
};
