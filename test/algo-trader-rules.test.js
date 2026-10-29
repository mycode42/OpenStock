'use strict';
// The shared Algo settings rules: OpenStock applies the same cases as the algo-trader Python tests.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { algoTraderRoot } = require('../lib/paths');
const { validateSettings } = require('../server/algo-trader/settings');
const { loadRules } = require('../server/algo-trader/settings-rules');

const root = algoTraderRoot();

function apply(settings, dotted, value) {
    const parts = dotted.split('.');
    let target = settings;
    for (const part of parts.slice(0, -1)) target = target[part];
    target[parts[parts.length - 1]] = value;
}

test('shared rule cases match the algo-trader Python suite', async () => {
    const defaults = JSON.parse(await fs.readFile(path.join(root, 'portfolio', 'algo-strategy-config.default.json'), 'utf8'));
    const cases = JSON.parse(await fs.readFile(path.join(root, 'tests', 'portfolio', 'algo-strategy-rule-cases.json'), 'utf8')).cases;
    const rules = await loadRules(root);
    for (const item of cases) {
        const settings = structuredClone(defaults);
        for (const [dotted, value] of Object.entries(item.set)) apply(settings, dotted, value);
        if (item.error === null) {
            assert.doesNotThrow(() => validateSettings(settings, defaults, rules), item.name);
        } else {
            assert.throws(() => validateSettings(settings, defaults, rules), { message: item.error }, item.name);
        }
    }
});
