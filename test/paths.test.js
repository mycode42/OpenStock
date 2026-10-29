const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const paths = require('../lib/paths');

const repo = path.resolve(__dirname, '..');

test('defaults resolve from the checkout, not the current directory', () => {
    const cwd = process.cwd();
    try {
        process.chdir(require('node:os').tmpdir());
        assert.equal(paths.databasePath({}), path.join(repo, 'database', 'openstock.db'));
        assert.equal(paths.configFilePath({}), path.join(repo, '.env'));
    } finally {
        process.chdir(cwd);
    }
});

test('relative environment values resolve from the checkout root', () => {
    assert.equal(paths.databasePath({ DATABASE_PATH: './data/test.db' }), path.join(repo, 'data', 'test.db'));
});

test('absolute environment values are kept as given', () => {
    assert.equal(paths.databasePath({ DATABASE_PATH: '/var/lib/openstock/db.sqlite' }), '/var/lib/openstock/db.sqlite');
});

test('blank environment values fall back to the default', () => {
    assert.equal(paths.databasePath({ DATABASE_PATH: '   ' }), path.join(repo, 'database', 'openstock.db'));
});

test('the Algo Trader subtree is the default location inside the checkout', () => {
    assert.equal(paths.algoTraderRoot({}), path.join(repo, 'algo-trader'));
    assert.equal(paths.algoTraderRoot({ ALGO_TRADER_ROOT: './elsewhere' }), path.join(repo, 'elsewhere'));
});
