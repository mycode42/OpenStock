const fs = require('node:fs/promises');
const { evaluateRules, loadRules } = require('./settings-rules');
const path = require('node:path');
const { randomUUID, createHash } = require('node:crypto');
const FALLBACK_DEFAULT_SETTINGS = require('../algo-strategy-config.default.json');
const { finite, readJson, safeObject } = require('./io');

function validateSettings(value, defaults = FALLBACK_DEFAULT_SETTINGS, rules) {
    if (!Array.isArray(rules)) throw new Error('Algo rule set must be loaded before validating settings');
    const settings = mergeConfig(defaults, safeObject(value));
    validateConfigShape(settings, defaults);
    evaluateRules(settings, defaults, rules);
    return settings;
}

async function loadSettings(root) {
    let defaults = FALLBACK_DEFAULT_SETTINGS;
    try {
        defaults = await readJson(path.join(root, 'portfolio', 'algo-strategy-config.default.json'));
    } catch (error) {
        if (error.code !== 'ENOENT') throw error;
    }
    const overrides = await readJson(path.join(root, 'data', 'algo-strategy-config.json'), { optional: true });
    const rules = await loadRules(root);
    return validateSettings(overrides || {}, defaults, rules);
}

async function writeSettings(root, settings) {
    const dataDir = path.join(root, 'data');
    await fs.mkdir(dataDir, { recursive: true });
    const destination = path.join(dataDir, 'algo-strategy-config.json');
    const temporary = path.join(dataDir, `.algo-strategy-config.${process.pid}.${randomUUID()}.tmp`);
    try {
        const handle = await fs.open(temporary, 'wx', 0o600);
        try {
            await handle.writeFile(`${JSON.stringify(settings, null, 2)}\n`, 'utf8');
            await handle.sync();
        } finally {
            await handle.close();
        }
        await fs.rename(temporary, destination);
    } catch (error) {
        await fs.rm(temporary, { force: true });
        throw error;
    }
}

function mergeConfig(base, override) {
    const result = { ...base };
    for (const [key, value] of Object.entries(safeObject(override))) {
        result[key] = value && typeof value === 'object' && !Array.isArray(value)
            && base[key] && typeof base[key] === 'object' && !Array.isArray(base[key])
            ? mergeConfig(base[key], value)
            : value;
    }
    return result;
}

function validateConfigShape(value, template, prefix = '') {
    if (template === null) {
        if (value !== null && (typeof value !== 'number' || !Number.isFinite(value))) {
            throw new Error(`${prefix} must be null or a finite number`);
        }
        return;
    }
    if (template && typeof template === 'object' && !Array.isArray(template)) {
        if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(`${prefix || 'settings'} must be an object`);
        for (const [key, item] of Object.entries(value)) {
            if (!Object.hasOwn(template, key)) {
                if (prefix === 'dividends') {
                    if (typeof item !== 'number' || !Number.isFinite(item) || item < 0) throw new Error(`dividends.${key} must be a finite non-negative number`);
                    continue;
                }
                throw new Error(`Unsupported setting: ${prefix ? `${prefix}.` : ''}${key}`);
            }
            validateConfigShape(item, template[key], prefix ? `${prefix}.${key}` : key);
        }
        return;
    }
    if (Array.isArray(template)) {
        if (!Array.isArray(value) || value.some(item => typeof item !== typeof template[0])) throw new Error(`${prefix} must be an array of ${typeof template[0]}`);
        if (value.some(item => typeof item === 'number' && !Number.isFinite(item))) throw new Error(`${prefix} values must be finite`);
        return;
    }
    if (typeof template === 'number') {
        if (typeof value !== 'number' || !Number.isFinite(value)) throw new Error(`${prefix} must be a finite number`);
        return;
    }
    if (typeof value !== typeof template) throw new Error(`${prefix} must be ${typeof template}`);
}

function validateOptionsConfig(config) {
    const nonNegative = [
        ['risk', 'max_risk_per_trade_pct'], ['risk', 'max_total_defined_risk_pct'], ['risk', 'max_daily_loss_pct'],
        ['risk', 'max_delta_notional_pct'], ['risk', 'max_vega_per_vol_point_pct'], ['risk', 'max_gamma_1pct_loss_pct'],
        ['risk', 'max_theta_loss_per_day_pct'], ['risk', 'max_margin_pct'], ['vrp', 'min_iv_minus_rv'],
        ['vrp', 'short_delta_min'], ['vrp', 'short_delta_max'], ['vrp', 'wing_delta_max'],
        ['llm_driven', 'min_move_edge_pct'], ['llm_driven', 'min_llm_confidence'],
    ];
    for (const [section, key] of nonNegative) {
        const value = safeObject(config[section])[key];
        if (value !== undefined && value < 0) throw new Error(`${section}.${key} must be non-negative`);
    }
    const maxIvToRealizedRatio = config.long_volatility?.max_iv_to_realized_ratio;
    if (maxIvToRealizedRatio !== undefined && maxIvToRealizedRatio !== null && maxIvToRealizedRatio <= 0) {
        throw new Error('long_volatility.max_iv_to_realized_ratio must be null or positive');
    }
    if (config.market_data_feed !== 'opra' && config.market_data_feed !== 'indicative') throw new Error('market_data_feed must be opra or indicative');
    if (config.risk.max_risk_per_trade_pct <= 0 || config.risk.max_contracts_per_trade < 1) throw new Error('risk budget and max_contracts_per_trade must be positive');
    if (config.vrp.short_delta_min > config.vrp.short_delta_max || config.vrp.max_dte < config.vrp.min_dte) throw new Error('VRP ranges are inverted');
    for (const [key, value] of Object.entries(config.regime_risk_multipliers || {})) {
        if (!Number.isFinite(value) || value < 0 || value > 2) throw new Error(`regime_risk_multipliers.${key} must be between 0 and 2`);
    }
    return config;
}

function optionsConfigPath(root) {
    const overridePath = process.env.ALPACA_OPTIONS_CONFIG;
    return overridePath ? path.resolve(root, overridePath) : path.join(root, 'data', 'options', 'config.json');
}

async function loadOptionsSettings(root) {
    const defaults = await readJson(path.join(root, 'options', 'config.default.json'));
    const override = await readJson(optionsConfigPath(root), { optional: true }) || {};
    const settings = mergeConfig(defaults, override);
    if (process.env.ALPACA_OPTIONS_FEED) settings.market_data_feed = process.env.ALPACA_OPTIONS_FEED.trim().toLowerCase();
    validateConfigShape(settings, defaults);
    return validateOptionsConfig(settings);
}

async function writeOptionsSettings(root, settings) {
    const defaults = await readJson(path.join(root, 'options', 'config.default.json'));
    const merged = mergeConfig(await loadOptionsSettings(root), settings);
    validateConfigShape(merged, defaults);
    validateOptionsConfig(merged);
    const destination = optionsConfigPath(root);
    await fs.mkdir(path.dirname(destination), { recursive: true, mode: 0o700 });
    const temporary = `${destination}.${process.pid}.${randomUUID()}.tmp`;
    try {
        const handle = await fs.open(temporary, 'wx', 0o600);
        try {
            await handle.writeFile(`${JSON.stringify(merged, null, 2)}\n`, 'utf8');
            await handle.sync();
        } finally {
            await handle.close();
        }
        await fs.rename(temporary, destination);
    } catch (error) {
        await fs.rm(temporary, { force: true });
        throw error;
    }
}

module.exports = {
    validateSettings,
    loadSettings,
    writeSettings,
    mergeConfig,
    validateConfigShape,
    validateOptionsConfig,
    optionsConfigPath,
    loadOptionsSettings,
    writeOptionsSettings,
};
