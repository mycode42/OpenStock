const path = require('node:path');
const { readCoordinatorState, readJson, safeObject } = require('../algo-trader/io');
const { attachEuroAmounts, equityStrategyPerformance, optionsStrategyPerformance } = require('../algo-trader/performance');
const { loadOptionsSettings, loadSettings } = require('../algo-trader/settings');
const { loadRules } = require('../algo-trader/settings-rules');
const { buildPayload } = require('../algo-trader/equity');
const { UNIVERSE_HISTORY_RANGES, buildUniversePayload, fetchAlpacaUniverseHistory } = require('../algo-trader/universe');
const { buildOptionsPayload } = require('../algo-trader/options');
const { LabError, ResearchLab } = require('../algo-trader/research-lab');
const { buildDataStatus } = require('../algo-trader/data-status');
const research = require('../algo-trader/research-control');

module.exports = function registerAlgoTraderRoutes({ app, root = require('../../lib/paths').algoTraderRoot(), now = () => new Date() }) {
    app.get('/api/algo-trader/universe/:symbol/history', async (req, res) => {
        const symbol = String(req.params?.symbol || '').trim().toUpperCase();
        const range = String(req.query?.range || '1d');
        if (!/^[A-Z0-9.-]{1,16}$/.test(symbol)) return res.status(400).json({ error: 'Invalid universe symbol' });
        if (!Object.hasOwn(UNIVERSE_HISTORY_RANGES, range)) return res.status(400).json({ error: 'Unsupported history range' });
        if (app.locals?.demoMode) {
            const history = app.locals.demoData.universeHistory(symbol, range, now());
            return history ? res.json(history) : res.status(404).json({ error: 'Symbol is not in the demo universe' });
        }
        try {
            const state = await readCoordinatorState(root);
            const universe = safeObject(safeObject(state.strategy_portfolio).universe);
            const symbols = Array.isArray(universe.tickers) ? universe.tickers.map(value => String(value || '').toUpperCase()) : [];
            if (!symbols.includes(symbol)) return res.status(404).json({ error: 'Symbol is not in the current Algo Trader universe' });
            res.set('Cache-Control', 'no-store');
            res.json(await fetchAlpacaUniverseHistory(root, symbol, range));
        } catch (error) {
            console.error(`Algo Trader universe history unavailable: ${error.message}`);
            res.status(503).json({ error: 'Alpaca universe history is unavailable' });
        }
    });

    app.get('/api/algo-trader/universe', async (_req, res) => {
        if (app.locals?.demoMode) return res.json(app.locals.demoData.universePayload(now()));
        try {
            res.set('Cache-Control', 'no-store');
            res.json(await buildUniversePayload(root));
        } catch (error) {
            console.error(`Algo Trader universe unavailable: ${error.message}`);
            res.status(503).json({ error: 'Algo Trader universe data is unavailable' });
        }
    });

    app.get('/api/algo-trader', async (req, res) => {
        const account = req.query?.account || 'equity';
        if (!['equity', 'options'].includes(account)) return res.status(400).json({ error: 'Unsupported Algo Trader account' });
        if (app.locals?.demoMode) return res.json(app.locals.demoData.algoPayload(account, now()));
        try { res.json(account === 'options' ? await buildOptionsPayload(root) : await buildPayload(root)); }
        catch (error) {
            console.error(`Algo Trader dashboard unavailable: ${error.message}`);
            res.status(503).json({ error: 'Algo Trader data is unavailable' });
        }
    });

    // Research lab: one job at a time, profiles, and promotion with rollback.
    // Without systemd (macOS, Docker) there is no nightly timer to wait for, so the guard reads "not running".
    const lab = new ResearchLab({
        root,
        nightlyRunning: async () => {
            try {
                return (await research.researchState()).running;
            } catch (error) {
                if (error?.code === 'ENOENT') return false;
                throw error;
            }
        },
    });
    const labRoute = (handler) => async (req, res) => {
        try {
            res.json(await handler(req, res));
        } catch (error) {
            if (error instanceof LabError) return res.status(error.status).json({ error: error.message });
            if (error.status >= 400 && error.status < 500) return res.status(error.status).json({ error: error.message });
            console.error(`Research lab request failed: ${error.message}`);
            res.status(503).json({ error: 'Research lab is unavailable' });
        }
    };
    const labWrite = (handler) => (req, res, next) => {
        if (app.locals?.demoMode) return res.status(409).json({ error: 'Research lab runs are disabled in demo mode.' });
        return labRoute(handler)(req, res, next);
    };
    app.get('/api/algo-trader/research/lab', labRoute(req => {
        const account = String(req.query?.account || 'options');
        if (app.locals?.demoMode) return app.locals.demoData.labOverview(account);
        return lab.overview(account);
    }));
    app.post('/api/algo-trader/research/jobs', labWrite(async (req, res) => {
        const job = await lab.startJob(req.body);
        res.status(202);
        return job;
    }));
    // Read-only status of the scheduled data jobs. Demo mode has no ThetaData or cron jobs to report.
    app.get('/api/algo-trader/research/data-status', async (_req, res) => {
        if (app.locals?.demoMode) return res.json(app.locals.demoData.dataStatusPayload(now()));
        try {
            res.set('Cache-Control', 'no-store');
            res.json(await buildDataStatus({ root, envPath: path.join(root, '..', '.env'), now: now() }));
        } catch (error) {
            console.error(`Algo data status unavailable: ${error.message}`);
            res.status(503).json({ error: 'Data status is unavailable' });
        }
    });

    app.get('/api/algo-trader/research/jobs/:id', labRoute(req => lab.jobStatus(req.params.id)));
    app.get('/api/algo-trader/research/jobs/:id/log', labRoute(req => lab.readLogChunk(req.params.id, req.query.offset)));
    app.get('/api/algo-trader/research/jobs/:id/results', labRoute(req => lab.jobResults(req.params.id)));
    app.post('/api/algo-trader/research/jobs/:id/cancel', labWrite(req => lab.cancelJob(req.params.id)));
    // The plan: which profile each strategy runs with, kept between visits.
    app.get('/api/algo-trader/research/plan', labRoute(req => lab.getPlan(String(req.query?.account || 'equity'))));
    app.put('/api/algo-trader/research/plan', labWrite(req => lab.setPlan(safeObject(req.body).account, safeObject(req.body).profiles)));
    app.get('/api/algo-trader/research/plan/preview', labRoute(req => {
        // The demo has no live settings to compare with, so its preview is empty.
        if (app.locals?.demoMode) return { account: String(req.query?.account || 'equity'), strategies: [], not_applied: [], changes: 0 };
        return lab.planPreview(String(req.query?.account || 'equity'));
    }));
    app.post('/api/algo-trader/research/plan/apply', labWrite(req => lab.planApply(safeObject(req.body).account || 'equity')));
    app.post('/api/algo-trader/research/plan/rollback', labWrite(req => lab.planRollback(safeObject(req.body).account || 'equity', { force: safeObject(req.body).force === true })));
    app.post('/api/algo-trader/research/profiles', labWrite(async req => ({
        profiles: await lab.saveProfile({ ...safeObject(req.body), source_job: safeObject(req.body).source_job || null }),
    })));
    app.post('/api/algo-trader/research/promote', labWrite(async req => {
        const body = safeObject(req.body);
        if (body.account !== 'options') throw new LabError('only options profiles can be promoted yet', 409);
        if (body.confirm !== true) return { ...(await lab.promotePreview(body.strategy, body.profile)), confirm_required: true };
        return lab.promote(body.strategy, body.profile);
    }));
    app.post('/api/algo-trader/research/rollback', labWrite(req => lab.rollback(safeObject(req.body).strategy)));
    // Nightly research timers and the kill switch. Changing them is refused in demo mode, like lab runs.
    app.get('/api/algo-trader/research/control', async (_req, res) => {
        if (app.locals?.demoMode) return res.json(research.demoState());
        try {
            res.set('Cache-Control', 'no-store');
            res.json(await research.researchState());
        } catch (error) {
            res.status(503).json({ error: `Research control is unavailable: ${error.message}` });
        }
    });
    app.post('/api/algo-trader/research/control/kill', labWrite(() => research.killRunningResearch()));
};

module.exports.buildPayload = buildPayload;
module.exports.equityStrategyPerformance = equityStrategyPerformance;
module.exports.optionsStrategyPerformance = optionsStrategyPerformance;
module.exports.attachEuroAmounts = attachEuroAmounts;
