const { createSystemLoad } = require('../system-load');

module.exports = function registerSystemRoutes({ app, root }) {
    const snapshot = createSystemLoad({ root });
    app.get('/api/system/load', async (req, res) => {
        try {
            // The public demo never shows this host's processes or memory. It returns sample figures in the same shape.
            if (req.app.locals?.demoMode) {
                return res.json({
                    at: new Date().toISOString(),
                    cpu: { overall: 12.4, cores: [9.1, 14.2, 11.8, 14.5] },
                    memory: { total_mb: 16000, used_mb: 6400, available_mb: 9600, percent: 40 },
                    swap: { total_mb: 2048, used_mb: 128, percent: 6 },
                    zram: { data_mb: 0, compressed_mb: 0, ram_used_mb: 0, ratio: 0 },
                    load: [0.42, 0.36, 0.31],
                    uptime_seconds: 86400,
                    processes: [
                        { pid: 1001, name: 'sample-service', cpu_percent: 2.1, rss_mb: 180 },
                        { pid: 1002, name: 'sample-worker', cpu_percent: 0.8, rss_mb: 96 },
                    ],
                    research: null,
                    demo: true,
                });
            }
            res.json(await snapshot());
        } catch (error) {
            console.error(`System load is unavailable: ${error.message}`);
            res.status(503).json({ error: 'System load is unavailable' });
        }
    });
};
