'use strict';

const fs = require('node:fs');

const EXIT = Object.freeze({ OK: 0, PARTIAL: 1, CONFIG: 2, LOCKED: 4 });

function safeError(error) {
    return String(error?.message || error || 'unknown error')
        .replace(/[\r\n\t]/g, ' ')
        .replace(/(bot)[^/\s]*/gi, '$1[redacted]')
        .slice(0, 300);
}

async function sendTelegram(message, { token, chatId, fetchImpl, timeoutMs = 10000 }) {
    if (!token || !chatId) throw new Error('Telegram recipient or bot token is not configured');
    const body = new URLSearchParams({ chat_id: String(chatId), text: message, parse_mode: 'HTML' });
    let response;
    try {
        response = await fetchImpl(`https://api.telegram.org/bot${encodeURIComponent(token)}/sendMessage`, {
            signal: AbortSignal.timeout(timeoutMs), method: 'POST', body,
            headers: { 'Content-Type': 'application/x-www-form-urlencoded', Accept: 'application/json' }
        });
    } catch (error) {
        const failure = error instanceof Error ? error : new Error('Telegram request failed before a response');
        failure.deliveryUnknown = true;
        throw failure;
    }
    if (!response?.ok) {
        const error = new Error(`Telegram request failed (${response?.status || 'unknown status'})`);
        if (!response || response.status >= 500) error.deliveryUnknown = true;
        throw error;
    }
    let payload;
    try { payload = await response.json(); }
    catch (error) { error.deliveryUnknown = true; throw error; }
    if (payload?.ok !== true) throw new Error('Telegram rejected message');
    return payload.result?.message_id ?? 'sent';
}

function acquireLock(lockPath, now = Date.now()) {
    const create = () => {
        const descriptor = fs.openSync(lockPath, 'wx', 0o600);
        try { fs.writeFileSync(descriptor, JSON.stringify({ pid: process.pid, createdAt: now }) + '\n'); }
        finally { fs.closeSync(descriptor); }
        let released = false;
        return () => {
            if (released) return;
            released = true;
            try { fs.unlinkSync(lockPath); } catch {}
        };
    };

    try { return create(); }
    catch (error) {
        if (error.code !== 'EEXIST') throw error;
        let owner = null;
        let ageMs = 0;
        try {
            owner = JSON.parse(fs.readFileSync(lockPath, 'utf8'));
            ageMs = Math.max(0, now - fs.statSync(lockPath).mtimeMs);
        } catch {
            try { ageMs = Math.max(0, now - fs.statSync(lockPath).mtimeMs); } catch {}
        }
        if (Number.isInteger(owner?.pid) && owner.pid > 0) {
            try { process.kill(owner.pid, 0); return null; }
            catch (processError) { if (processError.code === 'EPERM') return null; }
        } else if (ageMs < 30000) {
            return null;
        }
        fs.rmSync(lockPath, { recursive: true, force: true });
        try { return create(); }
        catch (retryError) { if (retryError.code === 'EEXIST') return null; throw retryError; }
    }
}

module.exports = { EXIT, safeError, sendTelegram, acquireLock };
