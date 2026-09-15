'use strict';

const fs = require('node:fs');
const fsp = require('node:fs/promises');
const path = require('node:path');
const { calculateDashboard, snapshotBucketAt } = require('./analytics');

const EXIT = Object.freeze({ OK: 0, PARTIAL: 1, CONFIG: 2, FX: 3, LOCKED: 4 });
const VALID_SOURCES = new Set(['yahoo_pre_close', 'yahoo_regular_close', 'yahoo_post_close', 'yahoo_extended_close', 'yahoo_regular_market_price']);

function finitePositive(value, label) {
    const number = Number(value);
    if (!Number.isFinite(number) || number <= 0) throw new Error(`Invalid ${label}`);
    return number;
}

function safeError(error) {
    return String(error?.message || error || 'unknown error')
        .replace(/[\r\n\t]/g, ' ')
        .replace(/(bot)[^/\s]*/gi, '$1[redacted]')
        .slice(0, 300);
}

function parseYahooQuote(payload) {
    if (!payload || typeof payload !== 'object') throw new Error('Yahoo response is not an object');
    const chart = payload.chart;
    const result = chart?.result?.[0];
    if (!result) throw new Error('Yahoo response contains no quote result');
    const meta = result.meta || {};
    const quote = result.indicators?.quote?.[0] || {};
    const closes = Array.isArray(quote.close) ? quote.close : [];
    const timestamps = Array.isArray(result.timestamp) ? result.timestamp : [];
    let price;
    let quoteTime = null;
    for (let index = closes.length - 1; index >= 0; index -= 1) {
        if (closes[index] !== null && Number.isFinite(Number(closes[index]))) {
            price = Number(closes[index]);
            quoteTime = Number.isFinite(Number(timestamps[index])) ? Number(timestamps[index]) : null;
            break;
        }
    }
    let session = 'extended';
    const periods = meta.currentTradingPeriod || {};
    if (quoteTime !== null) {
        for (const candidate of ['pre', 'regular', 'post']) {
            const period = periods[candidate] || {};
            if (Number(period.start) <= quoteTime && quoteTime <= Number(period.end)) { session = candidate; break; }
        }
    }
    let source = `yahoo_${session}_close`;
    if (price === undefined && Number.isFinite(Number(meta.regularMarketPrice))) {
        price = Number(meta.regularMarketPrice);
        source = 'yahoo_regular_market_price';
    }
    finitePositive(price, 'Yahoo price');
    return {
        priceUsd: price,
        quoteTime,
        source: VALID_SOURCES.has(source) ? source : 'unknown',
        name: String(meta.shortName || meta.longName || '').slice(0, 200)
    };
}

function parseTradeRepublicMonitorQuote(quote, now = new Date()) {
    if (!quote || typeof quote !== 'object') throw new Error('Trade Republic quote unavailable');
    const currency = String(quote.currency || '').toUpperCase();
    const bidUnit = String(quote.bid_unit || currency).toUpperCase();
    const askUnit = String(quote.ask_unit || currency).toUpperCase();
    if (currency !== 'EUR' || bidUnit !== 'EUR' || askUnit !== 'EUR') throw new Error('Trade Republic quote is not EUR');
    const bid = Number(quote.bid_eur);
    const ask = Number(quote.ask_eur);
    const validBid = Number.isFinite(bid) && bid > 0;
    const validAsk = Number.isFinite(ask) && ask > 0;
    if (!validBid && !validAsk) throw new Error('Trade Republic quote has no valid executable side');
    const priceEur = validBid && validAsk ? (bid + ask) / 2 : (validBid ? bid : ask);
    const quoteTime = quote.bid_time || quote.ask_time || quote.quote_time || now.toISOString();
    const parsedTime = new Date(quoteTime);
    return { priceEur: finitePositive(priceEur, 'Trade Republic EUR price'), quoteTime: Number.isNaN(parsedTime.getTime()) ? now.toISOString() : parsedTime.toISOString(), source: 'trade_republic_order_price' };
}

function readCache(cachePath, now = Date.now()) {
    try {
        const value = JSON.parse(fs.readFileSync(cachePath, 'utf8'));
        const rate = finitePositive(value.rate, 'cached exchange rate');
        const timestamp = Number(value.timestamp) * 1000;
        if (!Number.isFinite(timestamp)) throw new Error('invalid cache timestamp');
        return { rate, timestamp, fresh: now - timestamp >= 0 };
    } catch { return null; }
}

async function writeCache(cachePath, rate, now = Date.now()) {
    await fsp.mkdir(path.dirname(cachePath), { recursive: true });
    const temporary = `${cachePath}.${process.pid}.tmp`;
    await fsp.writeFile(temporary, JSON.stringify({ rate, timestamp: Math.floor(now / 1000) }) + '\n', { mode: 0o600 });
    await fsp.rename(temporary, cachePath);
}

async function fetchJson(url, { fetchImpl = globalThis.fetch, timeoutMs = 10000, headers = {}, method = 'GET', body } = {}) {
    if (typeof fetchImpl !== 'function') throw new Error('fetch is unavailable');
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
        const response = await fetchImpl(url, { method, headers, body, signal: controller.signal });
        if (!response?.ok) throw new Error(`HTTP ${response?.status || 'failure'} for ${String(url).slice(0, 120)}`);
        return await response.json();
    } finally { clearTimeout(timer); }
}

async function getExchangeRate({ cachePath, ttlMs, url, fetchImpl, now = Date.now(), log = () => {} }) {
    const cached = readCache(cachePath, now);
    if (cached && now - cached.timestamp < ttlMs) return { rate: cached.rate, source: 'cache' };
    try {
        const payload = await fetchJson(url, { fetchImpl, timeoutMs: 10000, headers: { Accept: 'application/json' } });
        const rate = finitePositive(payload?.rates?.EUR, 'exchange rate');
        await writeCache(cachePath, rate, now);
        return { rate, source: 'api' };
    } catch (error) {
        if (cached) { log(`FX API unavailable; using stale cached rate (${safeError(error)})`); return { rate: cached.rate, source: 'stale-cache' }; }
        throw new Error(`exchange rate unavailable: ${safeError(error)}`);
    }
}

function htmlEscape(value) {
    return String(value ?? '').replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character]));
}

function formatAlert(stock, trigger, priceEur, rate, now = new Date(), level = null, position = null) {
    const type = level?.side || (trigger === 'BUY_TRIGGERED' ? 'BUY' : 'SELL');
    const time = now.toISOString().slice(0, 16).replace('T', ' ');
    const icon = type === 'BUY' ? '🔼' : '🔽';
    const name = htmlEscape(stock.name || stock.symbol);
    const symbol = htmlEscape(stock.symbol);
    const target = Number(level?.price_eur ?? (type === 'BUY' ? stock.buy_threshold_eur : stock.sell_threshold_eur)).toFixed(2);
    return `<b>${icon} ${type} · ${name} (${symbol}) · €${target}</b>\nCurrent price: €${priceEur.toFixed(2)}\nPosition: ${Number(position?.quantity || 0)} shares · Journal${position?.owned ? '' : ' (none open)'}\nExchange rate: ${rate}\nTime: ${time} UTC`;
}

async function sendTelegram(message, { token, chatId, fetchImpl, timeoutMs = 10000 }) {
    if (!token || !chatId) throw new Error('Telegram recipient or bot token is not configured');
    const body = new URLSearchParams({ chat_id: String(chatId), text: message, parse_mode: 'HTML' });
    const payload = await fetchJson(`https://api.telegram.org/bot${encodeURIComponent(token)}/sendMessage`, {
        fetchImpl, timeoutMs, method: 'POST', body, headers: { 'Content-Type': 'application/x-www-form-urlencoded', Accept: 'application/json' }
    });
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

function createWriter(db) {
    const ledger = db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").get();
    if (!ledger || Number(db.prepare('SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations').get().version) < 6) throw new Error('Migrated alert-level schema is required before monitoring');
    for (const table of ['alert_levels', 'alerts_log', 'price_history', 'trades']) {
        if (!db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?").get(table)) throw new Error('Migrated schema 004 is required before monitoring');
    }
    const selectStocks = db.prepare(`SELECT id, symbol, name, price_source
        FROM stocks WHERE enabled = 1 AND archived = 0 ORDER BY symbol`);
    const updatePrice = db.prepare(`UPDATE stocks SET current_price_usd=?, current_price_eur=?, last_price_update=datetime('now'), last_exchange_rate=?, updated_at=datetime('now') WHERE id=?`);
    const history = db.prepare(`INSERT INTO price_history (stock_id, price_usd, price_eur, exchange_rate, source, quote_time, recorded_at)
        VALUES (?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))
        ON CONFLICT(stock_id, recorded_at) DO UPDATE SET price_usd=excluded.price_usd, price_eur=excluded.price_eur,
        exchange_rate=excluded.exchange_rate, source=excluded.source, quote_time=excluded.quote_time`);
    const updateName = db.prepare(`UPDATE stocks SET name=?, updated_at=datetime('now') WHERE id=? AND (name IS NULL OR name='')`);
    const levels = db.prepare('SELECT * FROM alert_levels WHERE stock_id=? AND enabled=1 AND active=1 AND triggered=0 AND retired=0 ORDER BY side, price_eur, id');
    const position = db.prepare('SELECT COALESCE(SUM(CASE WHEN closed_at IS NULL THEN quantity ELSE 0 END),0) quantity FROM trades WHERE stock_id=?');
    const retire = db.prepare("UPDATE alert_levels SET enabled=0, active=0, triggered=1, retired=1, triggered_at=datetime('now'), retired_at=datetime('now') WHERE id=? AND enabled=1 AND active=1 AND triggered=0 AND retired=0");
    const alert = db.prepare(`INSERT INTO alerts_log (stock_id, alert_level_id, alert_type, price_eur, threshold_eur, telegram_message_id) VALUES (?, ?, ?, ?, ?, ?)`);
    const mapping = db.prepare("SELECT isin, exchange, currency FROM broker_instrument_mappings WHERE stock_id=? AND broker='trade_republic' AND verified_at IS NOT NULL");
    return {
        stocks: () => selectStocks.all().map(stock => { const quantity = Number(position.get(stock.id).quantity || 0); return { ...stock, mapping: mapping.get(stock.id) || null, position: { source: 'journal', quantity, owned: quantity > 0, warning: quantity > 0 ? null : 'No open Journal position; SELL alerts are suppressed.' }, levels: levels.all(stock.id) }; }),
        recordQuote: (stock, quote, rate, recordedAt) => db.transaction(() => {
            updatePrice.run(quote.priceUsd, quote.priceUsd * rate, rate, stock.id);
            const quoteDate = quote.quoteTime ? (typeof quote.quoteTime === 'number' ? new Date(quote.quoteTime * 1000) : new Date(quote.quoteTime)) : null;
            history.run(stock.id, quote.priceUsd, quote.priceUsd * rate, rate, quote.source, quoteDate && !Number.isNaN(quoteDate.getTime()) ? quoteDate.toISOString().slice(0, 19).replace('T', ' ') : null, recordedAt);
            if (quote.name) updateName.run(quote.name, stock.id);
        })(),
        retireLevel: level => db.transaction(() => { alert.run(level.stock_id, level.id, level.side, level.price, level.price_eur, String(level.messageId)); retire.run(level.id); })()
    };
}

function recordEquity(db, now = new Date()) {
    const stocks = db.prepare('SELECT * FROM stocks ORDER BY symbol').all();
    const trades = db.prepare(`SELECT t.*, s.symbol, s.name, s.current_price_eur FROM trades t JOIN stocks s ON s.id=t.stock_id`).all();
    const capitalEvents = db.prepare('SELECT * FROM capital_events ORDER BY occurred_at, id').all();
    const snapshots = db.prepare('SELECT * FROM equity_snapshots ORDER BY snapshot_at').all();
    const analytics = calculateDashboard({ stocks, trades, capitalEvents, snapshots, now: now.toISOString() });
    if (!analytics.equity_available) return false;
    db.prepare(`INSERT INTO equity_snapshots (snapshot_at, cash_eur, position_value_eur, equity_eur, net_contributions_eur, financing_eur, source)
        VALUES (?, ?, ?, ?, ?, ?, 'monitor') ON CONFLICT(snapshot_at) DO UPDATE SET cash_eur=excluded.cash_eur,
        position_value_eur=excluded.position_value_eur, equity_eur=excluded.equity_eur, net_contributions_eur=excluded.net_contributions_eur,
        financing_eur=excluded.financing_eur, source=excluded.source`).run(snapshotBucketAt(now), analytics.capital.available_cash_eur, analytics.capital.invested_eur, analytics.capital.current_eur, analytics.capital.net_contributions_eur || 0, analytics.capital.financing_eur || 0);
    return true;
}

async function runMonitor({ db, config, fetchImpl = globalThis.fetch, now = new Date(), log = () => {}, error = () => {} }) {
    const release = acquireLock(config.lockPath);
    if (!release) return EXIT.LOCKED;
    let failures = 0;
    try {
        let fx;
        try {
            fx = await getExchangeRate({ ...config.fx, fetchImpl, now: now.getTime(), log });
        } catch (fxError) {
            error(`FX failed: ${safeError(fxError)}`);
            return EXIT.FX;
        }
        log(`USD/EUR rate: ${fx.rate} (${fx.source})`);
        const writer = createWriter(db);
        const stocks = writer.stocks();
        if (!stocks.length) return EXIT.OK;
        for (const stock of stocks) {
            try {
                log(`Processing ${stock.symbol} (id=${stock.id})`);
                let quote;
                if (stock.price_source === 'trade_republic') {
                    try {
                        if (!config.tradeRepublic) throw new Error('Trade Republic client unavailable');
                        if (!stock.mapping?.isin || !stock.mapping?.exchange || stock.mapping.currency !== 'EUR') throw new Error('verified Trade Republic mapping unavailable');
                        const brokerQuote = await config.tradeRepublic.getQuote({ isin: stock.mapping.isin, exchange: stock.mapping.exchange });
                        const monitorQuote = parseTradeRepublicMonitorQuote(brokerQuote, now);
                        quote = { priceUsd: monitorQuote.priceEur / fx.rate, quoteTime: monitorQuote.quoteTime, source: monitorQuote.source };
                    } catch (tradeRepublicError) {
                        log(`${stock.symbol}: Trade Republic price fallback (${safeError(tradeRepublicError)})`);
                    }
                }
                if (!quote) {
                    const payload = await fetchJson(`${config.yahooUrl}/${encodeURIComponent(stock.symbol)}?range=1d&interval=1m&includePrePost=true&corsDomain=finance.yahoo.com`, { fetchImpl, timeoutMs: config.timeoutMs, headers: { Accept: 'application/json', 'User-Agent': 'Mozilla/5.0 OpenStock/1.0' } });
                    quote = parseYahooQuote(payload);
                }
                const priceEur = quote.priceUsd * fx.rate;
                writer.recordQuote(stock, quote, fx.rate, now.toISOString().slice(0, 19).replace('T', ' '));
                const quoteDate = quote.quoteTime ? (typeof quote.quoteTime === 'number' ? new Date(quote.quoteTime * 1000) : new Date(quote.quoteTime)) : null;
                const quoteAt = quoteDate && !Number.isNaN(quoteDate.getTime()) ? quoteDate.toISOString() : 'unknown';
                log(`${stock.symbol} [${quote.source} @ ${quoteAt}]: $${quote.priceUsd} → €${priceEur.toFixed(4)} (position=${stock.position.quantity} ${stock.position.source})`);
                for (const level of stock.levels) {
                    const eligible = level.side === 'BUY' || stock.position.owned;
                    const hit = eligible && (level.side === 'BUY' ? priceEur <= level.price_eur : priceEur >= level.price_eur);
                    if (!hit) continue;
                    try {
                        const alertStock = { ...stock, name: stock.name || quote.name };
                        const id = await sendTelegram(formatAlert(alertStock, `${level.side}_TRIGGERED`, priceEur, fx.rate, now, level, stock.position), { ...config.telegram, fetchImpl });
                        writer.retireLevel({ ...level, price: priceEur, messageId: id });
                        log(`Telegram sent for ${stock.symbol}: ${id}`);
                    } catch (telegramError) { failures += 1; error(`Telegram failed for ${stock.symbol}: ${safeError(telegramError)}`); }
                }
            } catch (stockError) { failures += 1; error(`Stock ${stock.symbol} failed: ${safeError(stockError)}`); }
        }
        try { recordEquity(db, now); } catch (snapshotError) { failures += 1; error(`Equity snapshot failed: ${safeError(snapshotError)}`); }
        return failures ? EXIT.PARTIAL : EXIT.OK;
    } finally { release(); }
}

module.exports = { EXIT, parseYahooQuote, parseTradeRepublicMonitorQuote, readCache, writeCache, getExchangeRate, sendTelegram, formatAlert, acquireLock, createWriter, recordEquity, runMonitor, safeError };
