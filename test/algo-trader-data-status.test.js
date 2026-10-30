const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { buildDataStatus, envKeysPresent } = require('../server/algo-trader/data-status');

const SECRET = 'theta-secret-value-987';

function makeRoot(t, { envLine }) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-data-status-'));
    t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
    const root = path.join(dir, 'algo-trader');
    const theta = path.join(root, 'data', 'options', 'research', 'theta');
    fs.mkdirSync(path.join(theta, 'AAA'), { recursive: true });
    fs.mkdirSync(path.join(theta, 'BBB'), { recursive: true });
    fs.writeFileSync(path.join(theta, 'AAA', 'manifest.json'), JSON.stringify({ archive_complete: true }));
    fs.writeFileSync(path.join(theta, 'AAA', 'opening_manifest.json'), JSON.stringify({ archive_complete: true }));
    fs.writeFileSync(path.join(theta, 'BBB', 'manifest.json'), JSON.stringify({ archive_complete: false }));
    fs.writeFileSync(path.join(theta, 'pipeline_status.json'), JSON.stringify({
        phase: 'complete', end_date: '2026-10-29', symbols: 2, completed_at: '2026-10-30T10:00:00-04:00',
        updated_at: '2026-10-30T10:00:00-04:00', recent_pass_complete_at: '2026-10-30T09:00:00-04:00',
    }));
    const logs = path.join(root, 'data', 'algo', 'logs');
    fs.mkdirSync(logs, { recursive: true });
    fs.writeFileSync(path.join(logs, 'health.log'), 'ok line\nRuntimeError: cannot value holdings\n');
    fs.writeFileSync(path.join(logs, 'rotate.log'), 'rotated: none\n');
    const envPath = path.join(dir, '.env');
    fs.writeFileSync(envPath, `OTHER=1\n${envLine}\n`);
    return { root, envPath };
}

test('the status reports progress, the cancel action while the key is present, and never the key value', async t => {
    const { root, envPath } = makeRoot(t, { envLine: `THETA_DATA_API_KEY=${SECRET}` });
    const status = await buildDataStatus({ root, envPath, now: new Date('2026-10-30T10:05:00-04:00') });

    assert.equal(status.theta.phase, 'complete');
    assert.equal(status.theta.eod_complete, 1);
    assert.equal(status.theta.opening_complete, 1);
    assert.equal(status.theta.symbols, 2);
    assert.equal(status.theta.key_present, true);
    assert.match(status.theta.action, /Cancel the ThetaData subscription/);
    assert.equal(JSON.stringify(status).includes(SECRET), false);
    const health = status.logs.find(log => log.name === 'health.log');
    assert.equal(health.last_error, 'RuntimeError: cannot value holdings');
});

test('once the key is removed the action says so', async t => {
    const { root, envPath } = makeRoot(t, { envLine: 'THETA_DATA_API_KEY=' });
    const status = await buildDataStatus({ root, envPath, now: new Date() });
    assert.equal(status.theta.key_present, false);
    assert.match(status.theta.action, /removed from \.env/);
});

test('a missing pipeline reports not started with no logs and no failure', async t => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-data-status-empty-'));
    t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
    const status = await buildDataStatus({ root: path.join(dir, 'algo-trader'), envPath: path.join(dir, '.env'), now: new Date() });
    assert.equal(status.theta.phase, 'not_started');
    assert.equal(status.theta.action, null);
    assert.deepEqual(status.logs, []);
});

test('the env parser reads only the two ThetaData key names', () => {
    const values = envKeysPresent('THETADATA_API_KEY="abc"\nOTHER=zzz\n  THETA_DATA_API_KEY = def \n');
    assert.deepEqual(Object.keys(values).sort(), ['THETADATA_API_KEY', 'THETA_DATA_API_KEY']);
    assert.equal(values.THETADATA_API_KEY, 'abc');
});
