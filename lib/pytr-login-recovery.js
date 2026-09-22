'use strict';

const fs = require('node:fs/promises');
const path = require('node:path');
const { spawn: nativeSpawn } = require('node:child_process');

function boundedPositive(value, fallback, minimum, maximum) {
    const parsed = Number(value ?? fallback);
    return Number.isFinite(parsed) ? Math.min(Math.max(parsed, minimum), maximum) : fallback;
}

async function readState(fsImpl, statePath) {
    try { return JSON.parse(await fsImpl.readFile(statePath, 'utf8')); }
    catch (error) { if (error?.code === 'ENOENT') return null; throw error; }
}

async function writeState(fsImpl, statePath, state) {
    await fsImpl.mkdir(path.dirname(statePath), { recursive: true, mode: 0o700 });
    const temporary = `${statePath}.${process.pid}.${Date.now()}.tmp`;
    await fsImpl.writeFile(temporary, `${JSON.stringify(state)}\n`, { mode: 0o600 });
    await fsImpl.rename(temporary, statePath);
}

class PytrLoginRecovery {
    constructor({ statePath, cooldownMs = 15 * 60 * 1000, fsImpl = fs, now = () => new Date(), log = () => {} } = {}) {
        if (!statePath) throw new Error('PyTR login recovery state path is required');
        this.statePath = statePath;
        this.cooldownMs = boundedPositive(cooldownMs, 15 * 60 * 1000, 60 * 1000, 24 * 60 * 60 * 1000);
        this.fs = fsImpl;
        this.now = now;
        this.log = log;
    }

    async request() {
        const current = await readState(this.fs, this.statePath);
        const requestedAt = Date.parse(current?.requested_at || '');
        if (Number.isFinite(requestedAt) && this.now().getTime() - requestedAt < this.cooldownMs) {
            return { requested: false, reason: 'cooldown' };
        }
        const state = { status: 'requested', requested_at: this.now().toISOString() };
        await writeState(this.fs, this.statePath, state);
        this.log('PyTR v2 login recovery requested; awaiting Trade Republic app confirmation.');
        return { requested: true, reason: 'requested' };
    }
}

function waitForExit(spawn, command, args) {
    return new Promise((resolve, reject) => {
        let child;
        try { child = spawn(command, args, { stdio: 'ignore', env: { ...process.env, HOME: process.env.HOME || '/home/openclaw' } }); }
        catch (error) { reject(error); return; }
        child.once('error', reject);
        child.once('close', code => resolve(code));
    });
}

async function runPytrLoginRecovery({ statePath, command = process.env.PYTR_COMMAND || '/opt/pytr-venv/bin/pytr', fsImpl = fs, spawn = nativeSpawn, now = () => new Date(), log = () => {} } = {}) {
    if (!statePath) throw new Error('PyTR login recovery state path is required');
    const state = await readState(fsImpl, statePath);
    if (state?.status !== 'requested') return { started: false, reason: 'no_request' };
    const started = { ...state, status: 'started', started_at: now().toISOString() };
    await writeState(fsImpl, statePath, started);
    log('Starting PyTR v2 login recovery; confirmation in the Trade Republic app is required.');
    try {
        const code = await waitForExit(spawn, command, ['login', '--v2']);
        const finished = { ...started, status: code === 0 ? 'completed' : 'failed', finished_at: now().toISOString(), exit_code: Number.isInteger(code) ? code : null };
        await writeState(fsImpl, statePath, finished);
        log(code === 0 ? 'PyTR v2 login recovery completed.' : 'PyTR v2 login recovery was not confirmed; Yahoo fallback remains active.');
        return { started: true, completed: code === 0, exitCode: code };
    } catch {
        await writeState(fsImpl, statePath, { ...started, status: 'failed', finished_at: now().toISOString(), exit_code: null });
        log('PyTR v2 login recovery could not start; Yahoo fallback remains active.');
        return { started: true, completed: false, exitCode: null };
    }
}

module.exports = { PytrLoginRecovery, runPytrLoginRecovery, readState, writeState };
