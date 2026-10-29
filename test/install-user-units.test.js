'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

const script = path.join(__dirname, '..', 'scripts', 'install-user-units.sh');
const root = path.join(__dirname, '..');

test('user units render with no pytr, TR, or root settings', () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-units-'));
    const stubBin = path.join(dir, 'bin');
    fs.mkdirSync(stubBin);
    fs.writeFileSync(path.join(stubBin, 'systemctl'), '#!/bin/sh\nexit 0\n', { mode: 0o755 });
    execFileSync('sh', [script], {
        env: { ...process.env, XDG_CONFIG_HOME: dir, PATH: `${stubBin}:${process.env.PATH}` },
        stdio: 'pipe',
    });
    const target = path.join(dir, 'systemd', 'user');
    const names = fs.readdirSync(target).sort();
    assert.deepEqual(names, ['openstock-monitor.service', 'openstock-monitor.timer', 'openstock.service']);
    for (const name of names) {
        const text = fs.readFileSync(path.join(target, name), 'utf8');
        assert.doesNotMatch(text, /\{ROOT\}|\{NODE\}/);
        assert.doesNotMatch(text, /User=|Group=|pytr|tr-sdk|\/private|\/run\/openstock/);
        assert.ok(text.includes(`WorkingDirectory=${root}`) || name.endsWith('.timer'));
    }
    const monitor = fs.readFileSync(path.join(target, 'openstock-monitor.service'), 'utf8');
    assert.match(monitor, /MONITOR_LOCK_PATH=%t\/openstock-monitor\/monitor\.lock/);
    fs.rmSync(dir, { recursive: true, force: true });
});
