const test = require('node:test');
const assert = require('node:assert/strict');
const { minimalOverride, mergeConfig } = require('../server/algo-trader/settings');

const DEFAULTS = {
    vrp: { min_iv_minus_rv: 0.04, allowed_regimes: ['range_bound'], enabled: true },
    risk: { max_contracts_per_trade: 5, max_vega: 0.015 },
};

test('minimalOverride keeps only the leaves that differ from the defaults', () => {
    const settings = {
        vrp: { min_iv_minus_rv: 0.03, allowed_regimes: ['range_bound'], enabled: true },
        risk: { max_contracts_per_trade: 5, max_vega: 0.02 },
    };
    assert.deepEqual(minimalOverride(settings, DEFAULTS), { vrp: { min_iv_minus_rv: 0.03 }, risk: { max_vega: 0.02 } });
});

test('a value equal to the default is not stored, so later default changes still apply', () => {
    assert.deepEqual(minimalOverride({ vrp: { enabled: true } }, DEFAULTS), {});
});

test('defaults merged with the minimal override reproduce the full settings exactly', () => {
    const settings = {
        vrp: { min_iv_minus_rv: 0.03, allowed_regimes: ['trending_up'], enabled: true },
        risk: { max_contracts_per_trade: 1, max_vega: 0.015 },
        new_block: { flag: false },
    };
    const override = minimalOverride(settings, DEFAULTS);
    assert.deepEqual(mergeConfig(DEFAULTS, override), mergeConfig(DEFAULTS, settings));
});
