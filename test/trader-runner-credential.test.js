const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const { runnerToken } = require('../lib/trader-runner');

test('reads the runner token from the systemd credential directory', () => {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-runner-credential-'));
    const token = '0123456789abcdef0123456789abcdef';
    fs.writeFileSync(path.join(directory, 'trader-runner-api-token'), `${token}\n`, { mode: 0o600 });
    try {
        assert.equal(runnerToken({ CREDENTIALS_DIRECTORY: directory }), token);
    } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});

test('does not accept a short environment token', () => {
    assert.equal(runnerToken({ TRADER_RUNNER_API_TOKEN: 'short' }), '');
});
