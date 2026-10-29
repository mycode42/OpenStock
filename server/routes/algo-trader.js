const path = require('node:path');
const FALLBACK_DEFAULT_SETTINGS = require('../algo-strategy-config.default.json');
const { readCoordinatorState, readJson, safeObject } = require('../algo-trader/io');
const { attachEuroAmounts, equityStrategyPerformance, optionsStrategyPerformance } = require('../algo-trader/performance');
const { loadOptionsSettings, loadSettings, mergeConfig, validateSettings, writeOptionsSettings, writeSettings } = require('../algo-trader/settings');
const { loadRules } = require('../algo-trader/settings-rules');
const { buildPayload } = require('../algo-trader/equity');
const { UNIVERSE_HISTORY_RANGES, buildUniversePayload, fetchAlpacaUniverseHistory } = require('../algo-trader/universe');
const { buildOptionsPayload } = require('../algo-trader/options');

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

    app.put('/api/algo-trader/config', async (req, res) => {
        if (app.locals?.demoMode) return res.status(409).json({ error: 'Algo Trader settings are read-only in demo mode. Complete setup to edit live settings.' });
        const account = req.query?.account || req.body?.account || 'equity';
        if (!['equity', 'options'].includes(account)) return res.status(400).json({ error: 'Unsupported Algo Trader account' });
        try {
            if (account === 'options') {
                if (!req.body || !req.body.settings || typeof req.body.settings !== 'object' || Array.isArray(req.body.settings)) throw new Error('settings must be an object');
                await writeOptionsSettings(root, req.body.settings);
                res.json({ settings: await loadOptionsSettings(root), applied: 'next options paper scan' });
                return;
            }
            const current = await loadSettings(root);
            const body = safeObject(req.body);
            if (!req.body || !req.body.settings || typeof req.body.settings !== 'object' || Array.isArray(req.body.settings)) throw new Error('settings must be an object');
            const defaults = await readJson(path.join(root, 'portfolio', 'algo-strategy-config.default.json')).catch(error => {
                if (error.code !== 'ENOENT') throw error;
                return FALLBACK_DEFAULT_SETTINGS;
            });
            const settings = validateSettings(mergeConfig(current, body.settings), defaults, await loadRules(root));
            await writeSettings(root, settings);
            res.json({ settings, applied: 'next portfolio scan' });
        } catch (error) {
            const isValidationError = /must be|Unsupported setting|Unsupported Algo Trader account|inverted|positive|between/.test(error.message);
            if (!isValidationError) console.error(`Unable to save Algo Trader settings: ${error.message}`);
            res.status(isValidationError ? 400 : 503).json({ error: isValidationError ? error.message : 'Algo Trader settings could not be saved' });
        }
    });
};

module.exports.buildPayload = buildPayload;
module.exports.equityStrategyPerformance = equityStrategyPerformance;
module.exports.optionsStrategyPerformance = optionsStrategyPerformance;
module.exports.attachEuroAmounts = attachEuroAmounts;
