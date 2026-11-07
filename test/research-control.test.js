'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const control = require('../server/algo-trader/research-control');

// A fake systemctl and pgrep: records each call and answers from a small state table.
function fakeRunner(state) {
    const calls = [];
    const runner = async (file, args) => {
        calls.push([file, ...args].join(' '));
        if (file === 'pgrep') return state.pids.join('\n');
        if (file === 'ps') return state.commands.map(([pid, command]) => `${pid} ${command}`).join('\n');
        if (args[1] === 'show') {
            // The timer is asked for UnitFileState and ActiveState; a service only for ActiveState.
            const unit = args[2];
            const lines = [];
            if (unit.endsWith('.timer')) lines.push(`UnitFileState=${state.enabled[unit] ? 'enabled' : 'disabled'}`);
            lines.push(`ActiveState=${state.active[unit] ? 'active' : 'inactive'}`);
            return lines.join('\n');
        }
        return '';
    };
    return { runner, calls };
}

test('state reports each nightly timer as enabled and each service as running', async () => {
    const { runner } = fakeRunner({ enabled: { 'equity-research.timer': true }, active: { 'options-research.service': true }, pids: [], commands: [] });
    const state = await control.researchState({ runner });
    assert.equal(state.nightly.find(job => job.key === 'equity').enabled, true);
    assert.equal(state.nightly.find(job => job.key === 'options').enabled, false);
    assert.equal(state.nightly.find(job => job.key === 'options').running, true);
    assert.equal(state.running, true);
});

test('pausing and enabling a nightly job enables or disables its timer now', async () => {
    const fake = fakeRunner({ enabled: {}, active: {}, pids: [], commands: [] });
    await control.setNightlyEnabled('equity', true, { runner: fake.runner });
    await control.setNightlyEnabled('equity', false, { runner: fake.runner });
    assert.ok(fake.calls.includes('systemctl --user enable --now equity-research.timer'));
    assert.ok(fake.calls.includes('systemctl --user disable --now equity-research.timer'));
});

test('an unknown nightly job is refused before any systemctl call', async () => {
    const fake = fakeRunner({ enabled: {}, active: {}, pids: [], commands: [] });
    await assert.rejects(control.setNightlyEnabled('llm', true, { runner: fake.runner }), error => error.status === 400);
    assert.equal(fake.calls.length, 0);
});

test('kill stops running nightly services and terminates matching research processes', async () => {
    const fake = fakeRunner({ enabled: {}, active: { 'equity-research.service': true }, pids: [4242, 4343], commands: [[4242, 'python3'], [4343, 'bash']] });
    const signals = [];
    const result = await control.killRunningResearch({ runner: fake.runner, ownPid: 1, killImpl: (pid, signal) => signals.push([pid, signal]) });
    assert.deepEqual(signals, [[4242, 'SIGTERM']]);
    assert.deepEqual(result.stopped_services, ['equity-research.service']);
    assert.ok(fake.calls.includes('systemctl --user stop equity-research.service'));
    assert.deepEqual(result.terminated_pids, [4242]);  // the shell with the same words is left alone
});

test('the demo state lists both nightly switches and is not available for changes', () => {
    const state = control.demoState();
    assert.equal(state.available, false);
    assert.deepEqual(state.nightly.map(job => job.key), ['equity', 'options']);
});
