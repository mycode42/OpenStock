'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { PytrLoginRecovery, runPytrLoginRecovery } = require('../lib/pytr-login-recovery');

function memoryFs(initial = {}) {
    const files = new Map(Object.entries(initial));
    return {
        async readFile(file) { if (!files.has(file)) { const error = new Error('missing'); error.code = 'ENOENT'; throw error; } return files.get(file); },
        async mkdir() {},
        async writeFile(file, value) { files.set(file, value); },
        async rename(from, to) { files.set(to, files.get(from)); files.delete(from); },
        getJson(file) { return JSON.parse(files.get(file)); }
    };
}

test('pytr login recovery creates one private request per cooldown window', async () => {
    const statePath = '/state/pytr-login.json';
    const fsImpl = memoryFs();
    let now = new Date('2026-09-23T10:00:00Z');
    const recovery = new PytrLoginRecovery({ statePath, fsImpl, now: () => now, cooldownMs: 900000 });
    assert.deepEqual(await recovery.request(), { requested: true, reason: 'requested' });
    assert.deepEqual(await recovery.request(), { requested: false, reason: 'cooldown' });
    now = new Date('2026-09-23T10:15:01Z');
    assert.deepEqual(await recovery.request(), { requested: true, reason: 'requested' });
    assert.equal(fsImpl.getJson(statePath).status, 'requested');
});

test('pending request runs pytr login v2 without credentials and records completion', async () => {
    const statePath = '/state/pytr-login.json';
    const fsImpl = memoryFs({ [statePath]: JSON.stringify({ status: 'requested', requested_at: '2026-09-23T10:00:00Z' }) });
    let command, args, options;
    const spawn = (actualCommand, actualArgs, actualOptions) => {
        command = actualCommand; args = actualArgs; options = actualOptions;
        const child = new EventEmitter();
        queueMicrotask(() => child.emit('close', 0));
        return child;
    };
    const result = await runPytrLoginRecovery({ statePath, fsImpl, spawn, command: '/opt/pytr-venv/bin/pytr', now: () => new Date('2026-09-23T10:01:00Z') });
    assert.deepEqual(result, { started: true, completed: true, exitCode: 0 });
    assert.equal(command, '/opt/pytr-venv/bin/pytr');
    assert.deepEqual(args, ['login', '--v2']);
    assert.equal(options.stdio, 'ignore');
    assert.equal(fsImpl.getJson(statePath).status, 'completed');
});

test('no request never invokes pytr', async () => {
    const statePath = '/state/pytr-login.json';
    const fsImpl = memoryFs();
    const result = await runPytrLoginRecovery({ statePath, fsImpl, spawn: () => { throw new Error('must not spawn'); } });
    assert.deepEqual(result, { started: false, reason: 'no_request' });
});
