'use strict';
// Evaluates the shared rule file portfolio/algo-strategy-rules.json from the algo-trader repo.
// The same file is evaluated by portfolio/settings_rules.py in the live engine. Keep the two
// evaluators in step: both must accept and reject the same settings (see the shared cases in
// test/algo-trader-rules.test.js).

const fs = require('node:fs/promises');
const path = require('node:path');

const RULE_SCHEMA = 'algo_strategy_rules_v1';
const OPS = {
    '<=': (a, b) => a <= b,
    '<': (a, b) => a < b,
    '>=': (a, b) => a >= b,
    '>': (a, b) => a > b,
    '==': (a, b) => a === b,
};

async function loadRules(root) {
    const file = path.join(root, 'portfolio', 'algo-strategy-rules.json');
    let data;
    try {
        data = JSON.parse(await fs.readFile(file, 'utf8'));
    } catch (error) {
        throw new Error(`Algo rule set is unavailable at ${file}: ${error.code || error.message}`);
    }
    if (data?.schema !== RULE_SCHEMA || !Array.isArray(data.rules)) throw new Error(`Unexpected rule file schema in ${file}`);
    return data.rules;
}

function getPath(root, dotted) {
    let current = root;
    for (const part of dotted.split('.')) {
        if (!current || typeof current !== 'object' || !(part in current)) return undefined;
        current = current[part];
    }
    return current;
}

function expandPath(root, dotted, bindings = []) {
    const parts = dotted.split('.');
    const star = parts.indexOf('*');
    if (star === -1) return [{ path: dotted, bindings }];
    const head = parts.slice(0, star).join('.');
    const container = head ? getPath(root, head) : root;
    if (!container || typeof container !== 'object') return [];
    const rest = parts.slice(star + 1).join('.');
    return Object.keys(container).flatMap((key) => {
        const concrete = head ? `${head}.${key}` : key;
        return expandPath(root, rest ? `${concrete}.${rest}` : concrete, [...bindings, key]);
    });
}

const isNumber = (value) => typeof value === 'number' && Number.isFinite(value);

function operand(settings, value) {
    return typeof value === 'string' ? getPath(settings, value.replace(/^\$/, '')) : value;
}

function checkRule(rule, settings, defaults, dotted) {
    const value = dotted === null ? undefined : getPath(settings, dotted);
    switch (rule.check) {
        case 'range': {
            if (!isNumber(value)) return false;
            const low = rule.min;
            const high = operand(settings, rule.max);
            if (low !== undefined && (rule.min_exclusive ? value <= low : value < low)) return false;
            if (high !== undefined && high !== null && (rule.max_exclusive ? value >= high : value > high)) return false;
            return true;
        }
        case 'integer':
            if (!Number.isInteger(value)) return false;
            if (rule.min !== undefined && value < rule.min) return false;
            return rule.max === undefined || value <= rule.max;
        case 'member':
            return rule.values.includes(value);
        case 'equals_default':
            return value === getPath(defaults, dotted);
        case 'compare': {
            const left = operand(settings, rule.left);
            const right = operand(settings, rule.right);
            return isNumber(left) && isNumber(right) && OPS[rule.op](left, right);
        }
        case 'sum_le': {
            const terms = rule.terms.map((term) => operand(settings, term));
            const right = operand(settings, rule.right);
            return terms.every(isNumber) && isNumber(right) && terms.reduce((a, b) => a + b, 0) <= right;
        }
        default:
            throw new Error(`unknown rule check: ${rule.check}`);
    }
}

function renderMessage(rule, bindings) {
    let text = rule.message;
    if (bindings[0] !== undefined) text = text.replaceAll('{key}', bindings[0]);
    if (bindings[1] !== undefined) text = text.replaceAll('{regime}', bindings[1]);
    return text;
}

function evaluateRules(settings, defaults, rules) {
    for (const rule of rules) {
        const targets = rule.path === undefined
            ? [{ path: null, bindings: [] }]
            : expandPath(settings, rule.path);
        for (const { path: dotted, bindings } of targets) {
            if (!checkRule(rule, settings, defaults, dotted)) throw new Error(renderMessage(rule, bindings));
        }
    }
}

module.exports = { evaluateRules, loadRules };
