'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { startMonitorLoop } = require('../lib/monitor-cycle');

const quietLogger = { error() {}, log() {} };

test('the monitor loop never overlaps a slow cycle', async () => {
    let running = 0;
    let maxRunning = 0;
    let started = 0;
    const loop = startMonitorLoop({
        logger: quietLogger,
        firstDelayMs: 0,
        intervalMs: 5,
        runCycle: async () => {
            started += 1;
            running += 1;
            maxRunning = Math.max(maxRunning, running);
            await new Promise(resolve => setTimeout(resolve, 40));
            running -= 1;
        },
    });
    await new Promise(resolve => setTimeout(resolve, 150));
    loop.stop();
    await new Promise(resolve => setTimeout(resolve, 60));
    assert.ok(started >= 2, `expected several cycles, got ${started}`);
    assert.equal(maxRunning, 1);
});

test('a failing cycle is logged and the loop keeps going', async () => {
    const errors = [];
    let calls = 0;
    const loop = startMonitorLoop({
        logger: { log() {}, error: message => errors.push(message) },
        firstDelayMs: 0,
        intervalMs: 5,
        runCycle: async () => {
            calls += 1;
            throw new Error('Alpaca unreachable');
        },
    });
    await new Promise(resolve => setTimeout(resolve, 60));
    loop.stop();
    await new Promise(resolve => setTimeout(resolve, 20));
    assert.ok(calls >= 2, `expected the loop to retry, got ${calls}`);
    assert.ok(errors.every(message => message.includes('Alpaca unreachable')));
});

test('stop() prevents any further cycles', async () => {
    let calls = 0;
    const loop = startMonitorLoop({
        logger: quietLogger,
        firstDelayMs: 50,
        intervalMs: 5,
        runCycle: async () => { calls += 1; },
    });
    loop.stop();
    await new Promise(resolve => setTimeout(resolve, 80));
    assert.equal(calls, 0);
});
