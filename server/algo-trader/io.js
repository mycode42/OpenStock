const fs = require('node:fs/promises');
const path = require('node:path');
const { execFile } = require('node:child_process');
const Database = require('better-sqlite3');

function finite(value) {
    if (value === null || value === undefined || value === '') return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
}

function safeObject(value) {
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
}

function runPythonJson(root, script, env = {}) {
    const configuredPython = process.env.ALGO_TRADER_PYTHON;
    const executable = configuredPython || '/usr/local/bin/poetry';
    const args = configuredPython ? ['-c', script] : ['run', 'python', '-c', script];
    return new Promise((resolve, reject) => {
        execFile(executable, args, {
            cwd: root,
            env: { ...process.env, ...env },
            timeout: 90_000,
            maxBuffer: 4 * 1024 * 1024,
        }, (error, stdout) => {
            try {
                resolve(JSON.parse(stdout));
            } catch {
                reject(error || new Error('Algo Trader fill report returned invalid JSON'));
            }
        });
    });
}

async function readJson(filePath, { optional = false } = {}) {
    try {
        const value = JSON.parse(await fs.readFile(filePath, 'utf8'));
        if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Expected JSON object');
        return value;
    } catch (error) {
        if (optional && error.code === 'ENOENT') return null;
        throw error;
    }
}

function isoTimestamp(value) {
    if (!value) return null;
    const text = String(value).trim().replace(' ', 'T');
    const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(text) ? text : text;
    const parsed = new Date(normalized);
    return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString();
}

async function readCoordinatorState(root) {
    const dataDir = path.join(root, 'data');
    const databasePath = path.join(dataDir, 'algo_state.sqlite3');
    const db = new Database(databasePath, { readonly: true, fileMustExist: true });
    try {
        const row = db.prepare('SELECT market_state, state_json FROM coordinator WHERE id = 1').get();
        if (!row) throw new Error('Algo coordinator state is missing');
        const portfolio = JSON.parse(row.state_json);
        if (!portfolio || typeof portfolio !== 'object' || Array.isArray(portfolio)) {
            throw new Error('Algo coordinator portfolio is invalid');
        }
        return { market_state: row.market_state, strategy_portfolio: portfolio };
    } finally {
        db.close();
    }
}

async function readJsonLines(filePath, limit = 500) {
    let handle;
    try { handle = await fs.open(filePath, 'r'); }
    catch (error) { if (error.code === 'ENOENT') return []; throw error; }
    try {
        const stat = await handle.stat();
        const length = Math.min(stat.size, 2 * 1024 * 1024);
        const buffer = Buffer.alloc(length);
        await handle.read(buffer, 0, length, stat.size - length);
        let lines = buffer.toString('utf8').split(/\r?\n/);
        if (stat.size > length) lines = lines.slice(1);
        const rows = [];
        for (const line of lines.slice(-limit)) {
            try { const row = JSON.parse(line); if (row && typeof row === 'object' && !Array.isArray(row)) rows.push(row); }
            catch { /* Ignore a partial or malformed append-only row. */ }
        }
        return rows;
    } finally {
        await handle.close();
    }
}

module.exports = {
    finite,
    safeObject,
    readJson,
    readJsonLines,
    runPythonJson,
    isoTimestamp,
    readCoordinatorState,
};
