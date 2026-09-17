'use strict';

const { execFile: nativeExecFile } = require('node:child_process');
const { promisify } = require('node:util');
const execFile = promisify(nativeExecFile);

function safeError(error) {
    return String(error?.message || error || 'pytr rates request failed').replace(/[\r\n]+/g, ' ').slice(0, 240);
}

function parseRatesCsv(output, observedAt = new Date().toISOString()) {
    const rows = String(output || '').trim().split(/\r?\n/).filter(Boolean);
    const headerIndex = rows.findIndex(row => row === 'Name;ISIN;price;ask');
    if (headerIndex < 0) throw new Error('pytr did not return rates CSV');
    const quotes = new Map();
    for (const row of rows.slice(headerIndex + 1)) {
        const [name, isin, priceText, askText] = row.split(';');
        const price = Number(priceText);
        const ask = askText === undefined || askText === '' ? null : Number(askText);
        if (!/^[A-Z]{2}[A-Z0-9]{10}$/.test(String(isin || '')) || !Number.isFinite(price) || price <= 0 || (ask !== null && (!Number.isFinite(ask) || ask <= 0))) continue;
        quotes.set(isin, { isin, name: String(name || '').trim() || null, lastEur: price, askEur: ask, quoteTime: observedAt, observedAt, source: 'pytr_rates' });
    }
    if (!quotes.size) throw new Error('pytr returned no valid rates');
    return quotes;
}

class PytrRatesClient {
    constructor({ command = process.env.PYTR_COMMAND || '/opt/pytr-venv/bin/pytr', timeoutMs = Number(process.env.PYTR_TIMEOUT_MS || 8000), exec = execFile, now = () => new Date() } = {}) {
        this.command = command;
        this.timeoutMs = Math.min(Math.max(Number(timeoutMs) || 8000, 1000), 30000);
        this.exec = exec;
        this.now = now;
    }

    async getQuotes(isins) {
        const requested = [...new Set((isins || []).map(value => String(value || '').trim().toUpperCase()).filter(value => /^[A-Z]{2}[A-Z0-9]{10}$/.test(value)))];
        if (!requested.length) return new Map();
        try {
            // pytr uses a short-lived WebSocket subscription for each requested ISIN.
            // /dev/stdout keeps its stable semicolon CSV contract in-process without a shared temp file.
            const { stdout } = await this.exec(this.command, ['rates', '--no-waf-token', '--no-decimal-localization', '--sort-by-column', 'ISIN', '-o', '/dev/stdout', ...requested], { timeout: this.timeoutMs, maxBuffer: 1024 * 1024, env: { ...process.env, HOME: process.env.HOME || '/home/openclaw' } });
            return parseRatesCsv(stdout, this.now().toISOString());
        } catch (error) { throw new Error(`pytr unavailable: ${safeError(error)}`); }
    }
}

module.exports = { PytrRatesClient, parseRatesCsv };
