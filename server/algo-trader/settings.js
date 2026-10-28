const fs = require('node:fs/promises');
const path = require('node:path');
const { randomUUID, createHash } = require('node:crypto');
const FALLBACK_DEFAULT_SETTINGS = require('../algo-strategy-config.default.json');
const { finite, readJson, safeObject } = require('./io');

function validateSettings(value, defaults = FALLBACK_DEFAULT_SETTINGS) {
    const settings = mergeConfig(defaults, safeObject(value));
    validateConfigShape(settings, defaults);
    const pctPaths = [
        'general.risk_per_trade_pct', 'portfolio.max_name_pct', 'portfolio.max_position_pct',
        'portfolio.max_strategy_position_pct', 'portfolio.max_gross_pct', 'portfolio.max_net_long_pct',
        'portfolio.max_net_short_pct', 'portfolio.max_short_exposure_pct', 'portfolio.min_cash_pct',
        'portfolio.daily_loss_limit_pct', 'portfolio.weekly_loss_limit_pct', 'portfolio.no_buy_if_down_pct',
        'portfolio.max_tactical_pct',
    ];
    for (const pathName of pctPaths) {
        const percent = Number(pathName.split('.').reduce((value, key) => value?.[key], settings));
        if (!Number.isFinite(percent) || percent < 0 || percent > 1) throw new Error(pathName + ' must be between 0 and 1');
    }
    const portfolio = settings.portfolio;
    if (portfolio.max_gross_pct > 1 - portfolio.min_cash_pct) {
        throw new Error('portfolio.max_gross_pct cannot exceed the configured cash floor');
    }
    if (portfolio.max_position_pct > portfolio.max_strategy_position_pct
        || portfolio.max_net_long_pct > portfolio.max_gross_pct
        || portfolio.max_net_short_pct > portfolio.max_gross_pct
        || portfolio.max_tactical_pct > portfolio.max_gross_pct) {
        throw new Error('portfolio position and net exposure limits must fit within the configured gross cap');
    }
    if (portfolio.max_intraday_entries + portfolio.max_swing_entries > portfolio.max_daily_entries) {
        throw new Error('portfolio intraday and swing entry limits cannot exceed the daily entry limit');
    }
    if (settings.general.model_min_confidence < 0 || settings.general.model_min_confidence > 1) {
        throw new Error('general.model_min_confidence must be between 0 and 1');
    }
    if (settings.general.model_training_window < 252) {
        throw new Error('general.model_training_window must be at least 252');
    }
    if (settings.general.risk_per_trade_pct <= 0 || settings.general.risk_per_trade_pct > 0.02) {
        throw new Error('general.risk_per_trade_pct must be between 0 and 0.02');
    }
    if (settings.general.take_profit_r_multiple <= 0 || settings.general.take_profit_r_multiple > 10) {
        throw new Error('general.take_profit_r_multiple must be between 0 and 10');
    }
    if (settings.general.volatile_policy_risk_multiplier <= 0 || settings.general.volatile_policy_risk_multiplier > 1.5) {
        throw new Error('general.volatile_policy_risk_multiplier must be between 0 and 1.5');
    }
    if (!['committee', 'rule', 'hmm', 'gmm'].includes(settings.general.regime_engine)) {
        throw new Error('general.regime_engine must be committee, rule, hmm, or gmm');
    }
    if (!['core', 'technical', 'macro', 'marketwide'].includes(settings.general.regime_feature_set)) {
        throw new Error('general.regime_feature_set is unsupported');
    }
    for (const [key, value] of Object.entries(settings.general.volatility_risk_scales)) {
        if (!Number.isFinite(value) || value < 0 || value > 1.5) {
            throw new Error('general.volatility_risk_scales.' + key + ' must be between 0 and 1.5');
        }
    }
    const integerPaths = [
        'general.model_training_window',
        'portfolio.max_daily_entries', 'portfolio.max_intraday_entries', 'portfolio.max_swing_entries',
        'portfolio.max_intraday_core_entries', 'portfolio.max_open_positions',
        'portfolio.max_positions_per_strategy', 'portfolio.max_short_positions', 'portfolio.max_tactical_positions',
        'portfolio.swing_open_delay_minutes', 'portfolio.intraday_close_cutoff_minutes',
    ];
    for (const pathName of integerPaths) {
        const value = Number(pathName.split('.').reduce((item, key) => item?.[key], settings));
        if (!Number.isInteger(value) || value < 0) throw new Error(pathName + ' must be a non-negative integer');
    }
    for (const key of ['max_name_pct', 'max_position_pct', 'max_strategy_position_pct', 'max_gross_pct', 'max_net_long_pct', 'max_net_short_pct']) {
        if (portfolio[key] <= 0) throw new Error('portfolio.' + key + ' must be positive');
    }
    for (const [strategy, config] of Object.entries(settings.strategies)) {
        if (config.horizon !== defaults.strategies[strategy].horizon
            || config.bar_interval !== defaults.strategies[strategy].bar_interval) {
            throw new Error('Strategy horizon and bar interval are fixed by the catalog');
        }
        if (config.max_position_pct <= 0 || config.max_position_pct > portfolio.max_strategy_position_pct) {
            throw new Error('strategies.' + strategy + '.max_position_pct must be positive and within the portfolio strategy cap');
        }
        if (config.risk_multiplier <= 0 || config.risk_multiplier > 1.5) {
            throw new Error('strategies.' + strategy + '.risk_multiplier must be between 0 and 1.5');
        }
        if (config.hard_stop_pct <= 0 || config.hard_stop_pct > 0.5
            || config.trailing_stop_pct <= 0 || config.trailing_stop_pct > 0.5
            || config.trailing_activation_pct <= 0 || config.trailing_activation_pct > 0.5) {
            throw new Error('strategies.' + strategy + ' stop settings must be between 0 and 0.5');
        }
        for (const key of ['max_entries_per_day', 'max_positions', 'minimum_hold_minutes', 'cooldown_minutes']) {
            if (!Number.isInteger(config[key]) || config[key] < 0) {
                throw new Error('strategies.' + strategy + '.' + key + ' must be a non-negative integer');
            }
        }
        for (const [regime, weight] of Object.entries(config.regime_weights)) {
            if (!Number.isFinite(weight) || weight < 0 || weight > 1.5) {
                throw new Error('strategies.' + strategy + '.regime_weights.' + regime + ' must be between 0 and 1.5');
            }
        }
    }
    const eventConfidence = settings.strategies.swing_llm_driven.minimum_confidence;
    if (!Number.isInteger(eventConfidence) || eventConfidence < 0 || eventConfidence > 100) {
        throw new Error('strategies.swing_llm_driven.minimum_confidence must be an integer between 0 and 100');
    }
    for (const [name, value] of Object.entries(settings.portfolio.regime_gross_caps)) {
        if (!Number.isFinite(value) || value <= 0 || value > portfolio.max_gross_pct) {
            throw new Error('portfolio.regime_gross_caps.' + name + ' must be within the gross exposure cap');
        }
    }
    for (const [name, value] of Object.entries(settings.portfolio.sector_caps)) {
        if (!Number.isFinite(value) || value <= 0 || value > portfolio.max_gross_pct) {
            throw new Error('portfolio.sector_caps.' + name + ' must be within the gross exposure cap');
        }
    }
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
    return validateSettings(overrides || {}, defaults);
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
