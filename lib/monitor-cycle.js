'use strict';

// One monitoring cycle. The dashboard runs it on a 30-second loop, and `npm run monitor` runs it once by hand.
// Both write the same memory/monitor.log and memory/monitor-error.log.
const fs = require('node:fs');
const path = require('node:path');
const { EXIT, safeError } = require('./monitor');
const { runAlpacaMonitor } = require('./alpaca-monitor');

const APP_DIR = path.join(__dirname, '..');

function monitorPaths(env = process.env) {
    const memoryDir = env.MONITOR_MEMORY_DIR || path.join(APP_DIR, 'memory');
    return {
        memoryDir,
        logPath: env.MONITOR_LOG || path.join(memoryDir, 'monitor.log'),
        errorPath: env.MONITOR_ERROR_LOG || path.join(memoryDir, 'monitor-error.log'),
    };
}

function createMonitorLogger({ logPath, errorPath }) {
    fs.mkdirSync(path.dirname(logPath), { recursive: true });
    const lineFor = message => `[${new Date().toISOString()}] ${message}\n`;
    const log = message => {
        const line = lineFor(message);
        fs.appendFileSync(logPath, line);
        process.stdout.write(line);
    };
    const error = message => {
        const line = lineFor(`ERROR: ${message}`);
        fs.appendFileSync(errorPath, line);
        if (errorPath !== logPath) fs.appendFileSync(logPath, line);
        process.stdout.write(line);
    };
    return { log, error };
}

function positiveSetting(env, name, fallback) {
    const value = Number(env[name] ?? fallback);
    if (!Number.isFinite(value) || value <= 0) throw new Error(`${name} must be a positive number`);
    return value;
}

// makeClient({ timeoutMs }) builds the Alpaca client, so the caller decides how it is configured.
async function runMonitorCycle({ db, makeClient, logger, env = process.env }) {
    const { memoryDir } = monitorPaths(env);
    const chatId = env.TELEGRAM_CHAT_ID || '';
    if (!chatId) logger.error('No Telegram recipient configured; trigger notifications will fail safely');
    try {
        const timeoutMs = positiveSetting(env, 'MONITOR_TIMEOUT_MS', 10000);
        logger.log('=== Monitoring cycle started ===');
        const config = {
            timeoutMs,
            concurrency: positiveSetting(env, 'MONITOR_CONCURRENCY', 3),
            cycleDeadlineMs: positiveSetting(env, 'MONITOR_CYCLE_DEADLINE_MS', 45000),
            lockPath: env.MONITOR_LOCK_PATH || path.join(memoryDir, 'monitor.lock'),
            telegram: { token: env.TELEGRAM_BOT_TOKEN, chatId },
        };
        const code = await runAlpacaMonitor({ db, alpacaClient: makeClient({ timeoutMs }), log: logger.log, error: logger.error, config });
        logger.log(`Monitoring cycle complete (exit=${code})`);
        return code;
    } catch (monitorError) {
        logger.error(safeError(monitorError));
        return EXIT.CONFIG;
    }
}

// Runs cycles on a fixed interval. A cycle that is still running is never overlapped by the next tick.
function startMonitorLoop({ runCycle, logger, intervalMs = 30_000, firstDelayMs = 5_000 }) {
    let inFlight = null;
    const tick = () => {
        if (inFlight) return inFlight;
        inFlight = runCycle()
            .catch(loopError => logger.error(safeError(loopError)))
            .finally(() => { inFlight = null; });
        return inFlight;
    };
    const first = setTimeout(tick, firstDelayMs);
    const timer = setInterval(tick, intervalMs);
    return {
        stop() {
            clearTimeout(first);
            clearInterval(timer);
        },
    };
}

module.exports = { monitorPaths, createMonitorLogger, runMonitorCycle, startMonitorLoop };
