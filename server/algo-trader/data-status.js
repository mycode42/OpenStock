'use strict';

// Read-only status for the scheduled data jobs: the ThetaData history pipeline and the cron logs.
// Nothing here starts a job, calls a broker, or returns a secret. The key is reported only as present or absent.
const fs = require('node:fs/promises');
const path = require('node:path');
const { readJson } = require('./io');

const TAIL_BYTES = 64 * 1024;
// A real failure: a log level, a traceback, a named exception, or a gRPC error. Narrative words such as "failed"
// in a summary, and JSON fields that are empty or null, do not count.
const ERROR_LINE = /\b(ERROR|CRITICAL)\b|Traceback \(most recent|\b[A-Za-z_]*(Error|Exception):|INTERNAL:|debug_error_string = "/;
const EMPTY_ERROR_FIELD = /"[A-Za-z_]*(errors?|error_strings?)"\s*:\s*(null|\[\]|\{\}|""|0|false)/g;
// Only the last few lines describe the job's current state; an older failure is history.
const RECENT_LINES = 30;
const SUCCESS_LINE = /\b(ok|complete|completed|finished|nothing to do)\b/i;
// A summary such as "completed (0 errors)" is a success, not an error.
const ZERO_ERRORS = /\(0 errors?\)/gi;

async function countArchives(researchDir, fileName) {
    let entries = [];
    try { entries = await fs.readdir(researchDir, { withFileTypes: true }); }
    catch (error) { if (error.code === 'ENOENT') return { symbols: 0, complete: 0 }; throw error; }
    let symbols = 0;
    let complete = 0;
    for (const entry of entries) {
        if (!entry.isDirectory()) continue;
        const manifest = await readJson(path.join(researchDir, entry.name, fileName), { optional: true }).catch(() => null);
        if (!manifest) continue;
        symbols += 1;
        if (manifest.archive_complete === true) complete += 1;
    }
    return { symbols, complete };
}

async function thetaStatus(root, envValues) {
    const researchDir = path.join(root, 'data', 'options', 'research', 'theta');
    const pipeline = await readJson(path.join(researchDir, 'pipeline_status.json'), { optional: true }).catch(() => null);
    const eod = await countArchives(researchDir, 'manifest.json');
    const opening = await countArchives(researchDir, 'opening_manifest.json');
    const keyPresent = Boolean(envValues.THETA_DATA_API_KEY || envValues.THETADATA_API_KEY);
    // A finished backfill whose key has been removed is done: the block is hidden. Missing or incomplete history still shows.
    if (pipeline?.phase === 'complete' && !keyPresent) return null;
    return {
        phase: pipeline?.phase || 'not_started',
        end_date: pipeline?.end_date || null,
        symbols: Math.max(eod.symbols, Number(pipeline?.symbols) || 0),
        eod_complete: eod.complete,
        opening_complete: opening.complete,
        recent_pass_complete_at: pipeline?.recent_pass_complete_at || null,
        completed_at: pipeline?.completed_at || null,
        notify_pending: pipeline?.notify_pending === true,
        last_error: pipeline?.last_error || null,
        updated_at: pipeline?.updated_at || null,
        key_present: keyPresent,
        action: pipeline?.phase === 'complete'
            ? (keyPresent
                ? 'Cancel the ThetaData subscription, then remove THETA_DATA_API_KEY from .env.'
                : 'ThetaData is finished and its key is removed from .env.')
            : null,
    };
}

// Parse KEY=value lines from .env without keeping the values beyond the two keys we need.
function envKeysPresent(text) {
    const values = {};
    for (const line of String(text).split(/\r?\n/)) {
        const match = /^\s*(THETA_DATA_API_KEY|THETADATA_API_KEY)\s*=\s*(.*)$/.exec(line);
        if (match) values[match[1]] = match[2].replace(/^["']|["']$/g, '').trim();
    }
    return values;
}

async function readEnvValues(envPath) {
    try { return envKeysPresent(await fs.readFile(envPath, 'utf8')); }
    catch (error) { if (error.code === 'ENOENT') return {}; throw error; }
}

async function readTail(filePath, size) {
    const handle = await fs.open(filePath, 'r');
    try {
        const start = Math.max(0, size - TAIL_BYTES);
        const buffer = Buffer.alloc(size - start);
        await handle.read(buffer, 0, buffer.length, start);
        return buffer.toString('utf8');
    } finally {
        await handle.close();
    }
}

// ThetaData job logs are named theta-*.log. They are hidden once the history backfill is complete.
const THETA_LOG = /^theta-/;

async function logStatus(logDir, { hideThetaLogs = false } = {}) {
    let names = [];
    try { names = (await fs.readdir(logDir)).filter(name => name.endsWith('.log') && !(hideThetaLogs && THETA_LOG.test(name))).sort(); }
    catch (error) { if (error.code === 'ENOENT') return []; throw error; }
    const rows = [];
    for (const name of names) {
        const filePath = path.join(logDir, name);
        const stat = await fs.stat(filePath);
        const lines = stat.size ? (await readTail(filePath, stat.size)).split('\n').filter(Boolean) : [];
        const recent = lines.slice(-RECENT_LINES);
        const errorIndex = recent.findLastIndex(line => ERROR_LINE.test(line.replace(ZERO_ERRORS, '').replace(EMPTY_ERROR_FIELD, '')));
        // A later success line (complete, finished, ok, nothing to do) means the job recovered after that error.
        const recovered = errorIndex >= 0 && recent.slice(errorIndex + 1).some(line => SUCCESS_LINE.test(line));
        const lastError = errorIndex >= 0 && !recovered ? recent[errorIndex] : null;
        rows.push({
            name,
            size_bytes: stat.size,
            modified_at: stat.mtime.toISOString(),
            last_line: lines.at(-1)?.slice(0, 240) || null,
            last_error: lastError ? lastError.slice(0, 240) : null,
        });
    }
    return rows;
}

async function buildDataStatus({ root, envPath, now = new Date() }) {
    const envValues = await readEnvValues(envPath);
    const pipeline = await readJson(path.join(root, 'data', 'options', 'research', 'theta', 'pipeline_status.json'), { optional: true }).catch(() => null);
    return {
        generated_at: now.toISOString(),
        theta: await thetaStatus(root, envValues),
        logs: await logStatus(path.join(root, 'data', 'algo', 'logs'), { hideThetaLogs: pipeline?.phase === 'complete' }),
    };
}

module.exports = { buildDataStatus, envKeysPresent };
