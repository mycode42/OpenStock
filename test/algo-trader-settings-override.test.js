const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const { loadSettings, writeSettings } = require('../server/algo-trader/settings');

const REPO = path.resolve(__dirname, '..', 'algo-trader');

async function fixtureRoot(t) {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'algo-settings-'));
    t.after(() => fs.rm(root, { recursive: true, force: true }));
    await fs.mkdir(path.join(root, 'portfolio'), { recursive: true });
    for (const name of ['algo-strategy-config.default.json', 'algo-strategy-rules.json']) {
        await fs.copyFile(path.join(REPO, 'portfolio', name), path.join(root, 'portfolio', name)).catch(() => {});
    }
    return root;
}

test('the live settings are the fixed defaults file, and a write replaces it in full', async t => {
    const root = await fixtureRoot(t);
    const defaults = JSON.parse(await fs.readFile(path.join(root, 'portfolio', 'algo-strategy-config.default.json'), 'utf8'));
    const loaded = await loadSettings(root);
    assert.deepEqual(loaded, defaults);

    const changed = structuredClone(loaded);
    changed.strategies.swing_llm_driven.minimum_confidence = 81;
    await writeSettings(root, changed);

    const onDisk = JSON.parse(await fs.readFile(path.join(root, 'portfolio', 'algo-strategy-config.default.json'), 'utf8'));
    assert.deepEqual(onDisk, changed);
    await assert.rejects(fs.access(path.join(root, 'data', 'algo-strategy-config.json')));
});
