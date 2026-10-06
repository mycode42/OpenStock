'use strict';

const fs = require('node:fs');
const { calculateDashboard, snapshotBucketAt } = require('./analytics');
const { QuoteService } = require('./quote-service');
const { safeError, parseTradeRepublicMonitorQuote } = require('./market-data');
const { syncTradeRepublicExecutions } = require('./brokers/handelsrepublik-execution-sync');

const EXIT = Object.freeze({ OK: 0, PARTIAL: 1, CONFIG: 2, LOCKED: 4 });
const EUR_DISPLAY = new Intl.NumberFormat('de-DE', { minimumFractionDigits: 2, maximumFractionDigits: 6 });
const CRYPTO_EUR_DISPLAY = new Intl.NumberFormat('de-DE', { minimumFractionDigits: 2, maximumFractionDigits: 12 });
function htmlEscape(value) {
    return String(value ?? '').replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character]));
}

function formatAlert(stock, trigger, priceEur, now = new Date(), level = null, position = null, quote = null) {
    const type = level?.side || (trigger === 'BUY_TRIGGERED' ? 'BUY' : 'SELL');
    const time = now.toISOString().slice(0, 16).replace('T', ' ');
    const formatEur = stock.asset_type === 'crypto' ? CRYPTO_EUR_DISPLAY : EUR_DISPLAY;
    const name = htmlEscape(stock.name || stock.symbol);
    const symbol = htmlEscape(stock.symbol);
    const target = formatEur.format(Number(level?.price_eur ?? (type === 'BUY' ? stock.buy_threshold_eur : stock.sell_threshold_eur)));
    const move = level?.trigger_mode === 'PERCENT';
    const moveName = type === 'BUY' ? 'DROP' : 'GAIN';
    const icon = move ? (type === 'BUY' ? '📉' : '📈') : type === 'BUY' ? '🔼' : '🔽';
    const movePct = Number(level?.change_pct);
    const referencePrice = Number(level?.reference_price_eur);
    const triggerLabel = move && Number.isFinite(movePct) && movePct > 0
        ? `${movePct}% threshold from €${formatEur.format(referencePrice)}`
        : `€${target}`;
    const provider = quote?.source === 'trade_republic_sdk_order_price' ? 'Handelsrepublik SDK' : 'Trade Republic UAPI';
    const reference = quote?.source?.startsWith('trade_republic') ? `${provider} ${type === 'BUY' ? 'ask' : 'bid'}: €${formatEur.format(priceEur)}` : `Current price: €${formatEur.format(priceEur)}`;
    const moveProgress = move && referencePrice > 0 ? `\nMove from reference: ${(((priceEur / referencePrice) - 1) * 100).toFixed(2)}% · target €${target}` : '';
    const units = stock.asset_type === 'crypto' ? 'crypto units' : 'shares';
    return `<b>${icon} ${move ? moveName : type} · ${name} (${symbol}) · ${triggerLabel}</b>\n${reference}${moveProgress}\nPosition: ${Number(position?.quantity || 0)} ${units} · Journal${position?.owned ? '' : ' (none open)'}\nTime: ${time} UTC`;
}

function formatSdkConnectivityAlert(online, now = new Date()) {
    const time = now.toISOString().slice(0, 16).replace('T', ' ');
    return online
        ? `<b>✅ Handelsrepublik SDK available again</b>\nPrimary quote source restored.\nTime: ${time} UTC`
        : `<b>⚠️ Handelsrepublik SDK unavailable</b>\nOpenStock is using the UAPI fallback where available. Confirm any pending login in the Trade Republic app.\nTime: ${time} UTC`;
}

function formatQuoteFeedAlert(online, source = null, now = new Date()) {
    const time = now.toISOString().slice(0, 16).replace('T', ' ');
    return online
        ? `<b>✅ Live quotes restored</b>\nMonitoring resumed using ${source === 'sdk' ? 'Handelsrepublik SDK' : 'UAPI fallback'}.\nTime: ${time} UTC`
        : `<b>🚨 No live quote source available</b>\nOpenStock could not get live prices from any configured source. Price monitoring is paused until quotes recover. Confirm any pending SDK login in the Trade Republic app.\nTime: ${time} UTC`;
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

function updateProviderHealth(db, provider, online, now) {
    const checkedAt = now.toISOString();
    const status = online ? 'ONLINE' : 'OFFLINE';
    const failureCode = online ? null : (provider === 'trade_republic_sdk' ? 'SDK_UNAVAILABLE' : 'NO_QUOTE_SOURCE');
    const previous = db.prepare('SELECT status, last_notified_status FROM broker_health_state WHERE broker=?').get(provider);
    if (!previous) {
        db.prepare(`INSERT INTO broker_health_state (broker,status,checked_at,changed_at,last_notified_status,last_error)
            VALUES (?,?,?,?,?,?)`).run(provider, status, checkedAt, checkedAt, online ? status : null, failureCode);
    } else {
        db.prepare(`UPDATE broker_health_state SET status=?,checked_at=?,changed_at=CASE WHEN status<>? THEN ? ELSE changed_at END,
            last_error=? WHERE broker=?`).run(status, checkedAt, status, checkedAt, failureCode, provider);
    }
    const current = db.prepare('SELECT last_notified_status FROM broker_health_state WHERE broker=?').get(provider);
    return { status, shouldNotify: current.last_notified_status !== status };
}

async function sendHealthNotification(db, provider, status, message, { telegram, fetchImpl, now, log }) {
    const checkedAt = now.toISOString();
    try {
        const messageId = await sendTelegram(message, { ...telegram, fetchImpl });
        db.prepare('UPDATE broker_health_state SET last_notified_at=?,last_notified_status=? WHERE broker=?').run(checkedAt, status, provider);
        log(`${provider} ${status.toLowerCase()} notification sent: ${messageId}`);
        return { notified: true };
    } catch (notificationError) {
        return { notified: false, notificationError };
    }
}

async function checkQuoteAvailability({ db, quoteRows, sdkEnabled, telegram = {}, fetchImpl = globalThis.fetch, now = new Date(), log = () => {} }) {
    if (!db || typeof db.prepare !== 'function') return { checked: false };
    const sdkOnline = quoteRows.some(({ quote }) => quote?.source === 'trade_republic_sdk_order_price');
    const quote = quoteRows.find(row => row.quote)?.quote;
    const feedOnline = Boolean(quote);
    const feed = updateProviderHealth(db, 'trade_republic_quotes', feedOnline, now);
    let notificationResult = { notified: false };

    if (!feedOnline) {
        if (sdkEnabled) {
            updateProviderHealth(db, 'trade_republic_sdk', false, now);
            db.prepare("UPDATE broker_health_state SET last_notified_status='OFFLINE' WHERE broker='trade_republic_sdk'").run();
        }
        if (feed.shouldNotify) notificationResult = await sendHealthNotification(db, 'trade_republic_quotes', feed.status, formatQuoteFeedAlert(false, null, now), { telegram, fetchImpl, now, log });
        return { checked: true, feedOnline, sdkOnline, ...notificationResult };
    }

    if (sdkEnabled) {
        const sdk = updateProviderHealth(db, 'trade_republic_sdk', sdkOnline, now);
        if (sdk.shouldNotify) notificationResult = await sendHealthNotification(db, 'trade_republic_sdk', sdk.status, formatSdkConnectivityAlert(sdkOnline, now), { telegram, fetchImpl, now, log });
    }
    if (feed.shouldNotify) {
        notificationResult = await sendHealthNotification(db, 'trade_republic_quotes', feed.status, formatQuoteFeedAlert(true, sdkOnline ? 'sdk' : 'uapi', now), { telegram, fetchImpl, now, log });
    }
    return { checked: true, feedOnline, sdkOnline, ...notificationResult };
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
    if (!ledger || Number(db.prepare('SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations').get().version) < 18) throw new Error('Migrated alert-level schema and reliable monitoring migration are required before monitoring');
    for (const table of ['alert_levels', 'alerts_log', 'alert_outbox', 'price_history', 'trades']) {
        if (!db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?").get(table)) throw new Error('Migrated schema 004 is required before monitoring');
    }
    const selectStocks = db.prepare(`SELECT id, symbol, name, asset_type
        FROM stocks WHERE enabled = 1 AND archived = 0 ORDER BY symbol`);
    const updatePrice = db.prepare(`UPDATE stocks SET current_price_usd=NULL, current_price_eur=?, current_bid_eur=?, current_ask_eur=?, current_midpoint_eur=?, last_price_update=datetime('now'), last_exchange_rate=NULL, updated_at=datetime('now') WHERE id=?`);
    const history = db.prepare(`INSERT INTO price_history (stock_id, price_usd, price_eur, exchange_rate, source, quote_time, recorded_at)
        VALUES (?, NULL, ?, NULL, ?, ?, COALESCE(?, datetime('now')))
        ON CONFLICT(stock_id, recorded_at) DO UPDATE SET price_usd=excluded.price_usd, price_eur=excluded.price_eur,
        exchange_rate=excluded.exchange_rate, source=excluded.source, quote_time=excluded.quote_time`);
    const updateName = db.prepare(`UPDATE stocks SET name=?, updated_at=datetime('now') WHERE id=? AND (name IS NULL OR name='')`);
    const levels = db.prepare('SELECT * FROM alert_levels WHERE stock_id=? AND enabled=1 AND active=1 AND triggered=0 AND retired=0 ORDER BY side, price_eur, id');
    const position = db.prepare('SELECT COALESCE(SUM(CASE WHEN closed_at IS NULL AND quantity>1e-12 THEN quantity ELSE 0 END),0) quantity FROM trades WHERE stock_id=?');
    const retire = db.prepare("UPDATE alert_levels SET enabled=0, active=0, triggered=1, retired=1, triggered_at=datetime('now'), retired_at=datetime('now') WHERE id=? AND enabled=1 AND active=1 AND triggered=0 AND retired=0");
    const alert = db.prepare(`INSERT INTO alerts_log (stock_id, alert_level_id, alert_type, price_eur, threshold_eur, telegram_message_id) SELECT ?, ?, ?, ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM alerts_log WHERE alert_level_id=?)`);
    const createOutbox = db.prepare(`INSERT OR IGNORE INTO alert_outbox(alert_level_id,delivery_key,stock_id,message_html,price_eur,status) VALUES (?,?,?,?,?,'PENDING')`);
    const getOutbox = db.prepare('SELECT * FROM alert_outbox WHERE alert_level_id=?');
    const sending = db.prepare("UPDATE alert_outbox SET status='SENDING',attempt_count=attempt_count+1,last_attempt_at=datetime('now'),last_error=NULL,updated_at=datetime('now') WHERE id=? AND status IN ('PENDING','FAILED')");
    const sent = db.prepare("UPDATE alert_outbox SET status='SENT',telegram_message_id=?,sent_at=datetime('now'),updated_at=datetime('now') WHERE id=?");
    const failed = db.prepare("UPDATE alert_outbox SET status='FAILED',last_error=?,updated_at=datetime('now') WHERE id=?");
    const uncertain = db.prepare("UPDATE alert_outbox SET last_error=?,updated_at=datetime('now') WHERE id=? AND status='SENDING'");
    const mapping = db.prepare("SELECT isin, exchange, currency, verified_at FROM broker_instrument_mappings WHERE stock_id=? AND broker='trade_republic' AND verified_at IS NOT NULL");
    const brokerQuote = db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name='broker_quotes'").get() ? db.prepare(`INSERT INTO broker_quotes (broker,isin,exchange,bid_eur,ask_eur,last_eur,currency,quote_time,observed_at,source) VALUES ('trade_republic',?,?,?,?,?,?,?,?,?)`) : null;
    return {
        stocks: () => selectStocks.all().map(stock => { const quantity = Number(position.get(stock.id).quantity || 0); return { ...stock, mapping: mapping.get(stock.id) || null, position: { source: 'journal', quantity, owned: quantity > 0, warning: quantity > 0 ? null : 'No open Journal position; SELL alerts are suppressed.' }, levels: levels.all(stock.id) }; }),
        recordQuote: (stock, quote, recordedAt) => db.transaction(() => {
            updatePrice.run(quote.priceEur, quote.bidEur ?? null, quote.askEur ?? null, quote.priceEur, stock.id);
            const quoteDate = quote.quoteTime ? (typeof quote.quoteTime === 'number' ? new Date(quote.quoteTime * 1000) : new Date(quote.quoteTime)) : null;
            history.run(stock.id, quote.priceEur, quote.source, quoteDate && !Number.isNaN(quoteDate.getTime()) ? quoteDate.toISOString().slice(0, 19).replace('T', ' ') : null, recordedAt);
            if (quote.name) updateName.run(quote.name, stock.id);
            if (brokerQuote && stock.mapping && quote.source.startsWith('trade_republic')) brokerQuote.run(stock.mapping.isin, stock.mapping.exchange, quote.bidEur ?? null, quote.askEur ?? null, quote.lastEur ?? null, 'EUR', quote.quoteTime, new Date().toISOString(), quote.source);
        })(),
        prepareAlert: (level, price, message) => {
            const key = `level:${level.id}`;
            createOutbox.run(level.id, key, level.stock_id, message, price);
            return getOutbox.get(level.id);
        },
        startDelivery: outbox => sending.run(outbox.id).changes === 1,
        failDelivery: (outbox, deliveryError) => failed.run(safeError(deliveryError), outbox.id),
        uncertainDelivery: (outbox, deliveryError) => uncertain.run(`Delivery outcome unknown: ${safeError(deliveryError)}`, outbox.id),
        confirmDelivery: (outbox, level, messageId) => db.transaction(() => {
            sent.run(String(messageId), outbox.id);
            alert.run(level.stock_id, level.id, level.side, outbox.price_eur, level.price_eur, String(messageId), level.id);
            retire.run(level.id);
        })()
    };
}

async function mapBounded(items, limit, task) {
    let cursor = 0;
    const workers = Array.from({ length: Math.min(Math.max(1, limit), items.length) }, async () => {
        while (cursor < items.length) { const item = items[cursor++]; await task(item); }
    });
    await Promise.all(workers);
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

async function runMonitor({ db, journalDb = db, config, fetchImpl = globalThis.fetch, now = new Date(), log = () => {}, error = () => {} }) {
    const release = acquireLock(config.lockPath);
    if (!release) return EXIT.LOCKED;
    let failures = 0;
    try {
        const writer = createWriter(db);
        if (config.tradeRepublicSdk?.isEnabled?.() && config.tradeRepublicSdk?.sessionFile) {
            try {
                const synced = await syncTradeRepublicExecutions({ db: journalDb, sdkClient: config.tradeRepublicSdk, now: () => now });
                if (!synced.skipped) log(`Trade journal sync: imported=${synced.imported}, matched-manual=${synced.matched_manual || 0}, already-recorded=${synced.already_recorded}, oversells-needing-review=${synced.skipped_oversells}, incomplete=${synced.incomplete_fills}`);
            } catch (syncError) {
                failures += 1;
                error(`Trade Republic journal sync failed: ${safeError(syncError)}`);
            }
        }
        const stocks = writer.stocks();
        if (!stocks.length) { config.tradeRepublicSdk?.close?.(); return failures ? EXIT.PARTIAL : EXIT.OK; }
        const deadline = Date.now() + Math.max(1000, Number(config.cycleDeadlineMs || 45000));
        const quoteService = new QuoteService({ db: {}, brokerClient: config.tradeRepublic, sdkClient: config.tradeRepublicSdk, now: () => now, log, config });
        let quoteRows;
        try { quoteRows = await quoteService.fetchMany(stocks, { concurrency: config.concurrency || 3, deadlineAt: deadline }); }
        finally { config.tradeRepublicSdk?.close?.(); }
        const quoteFeedChecked = quoteRows.some(({ stock }) => stock.mapping?.verified_at && stock.mapping.currency === 'EUR');
        if (quoteFeedChecked) {
            const connectivity = await checkQuoteAvailability({ db, quoteRows, sdkEnabled: Boolean(config.tradeRepublicSdk?.isEnabled?.()), telegram: config.telegram, fetchImpl, now, log });
            if (connectivity.notificationError) {
                failures += 1;
                error(`Quote source status notification failed: ${safeError(connectivity.notificationError)}`);
            }
        }
        await mapBounded(quoteRows, Number(config.concurrency || 3), async ({ stock, quote, error: quoteError }) => {
            try {
                if (Date.now() >= deadline) throw new Error('monitor cycle deadline reached before quote request');
                log(`Processing ${stock.symbol} (id=${stock.id})`);
                if (!quote) throw quoteError || new Error('no quote available');
                if (quote.mapping && typeof db.prepare === 'function' && quote.source.startsWith('trade_republic')) db.addBrokerQuote?.({ broker: 'trade_republic', isin: quote.mapping.isin, exchange: quote.mapping.exchange, bid_eur: quote.bidEur, ask_eur: quote.askEur, last_eur: quote.lastEur || null, currency: 'EUR', quote_time: quote.quoteTime, observed_at: quote.observedAt, source: quote.source });
                const priceEur = quote.priceEur;
                writer.recordQuote(stock, quote, now.toISOString().slice(0, 19).replace('T', ' '));
                const quoteDate = quote.quoteTime ? (typeof quote.quoteTime === 'number' ? new Date(quote.quoteTime * 1000) : new Date(quote.quoteTime)) : null;
                const quoteAt = quoteDate && !Number.isNaN(quoteDate.getTime()) ? quoteDate.toISOString() : 'unknown';
                log(`${stock.symbol} [${quote.source} @ ${quoteAt}]: €${priceEur}${quote.bidEur !== undefined ? ` (bid=${quote.bidEur ?? 'n/a'} ask=${quote.askEur ?? 'n/a'})` : ''} (position=${stock.position.quantity} ${stock.position.source})`);
                for (const level of stock.levels) {
                    const eligible = level.trigger_mode === 'PERCENT' || level.side === 'BUY' || stock.position.owned;
                    const executable = level.side === 'BUY' ? (quote.askEur ?? priceEur) : (quote.bidEur ?? priceEur);
                    const hit = eligible && (level.side === 'BUY' ? executable <= level.price_eur : executable >= level.price_eur);
                    if (!hit) continue;
                    try {
                        const alertStock = { ...stock, name: stock.name || quote.name };
                        const message = formatAlert(alertStock, `${level.side}_TRIGGERED`, executable, now, level, stock.position, quote);
                        const outbox = writer.prepareAlert(level, executable, message);
                        if (outbox.status === 'SENT') { writer.confirmDelivery(outbox, level, outbox.telegram_message_id); continue; }
                        if (!writer.startDelivery(outbox)) {
                            failures += 1;
                            error(`Telegram outcome for ${stock.symbol} is uncertain; alert level ${level.id} remains frozen for manual review`);
                            continue;
                        }
                        const id = await sendTelegram(outbox.message_html, { ...config.telegram, fetchImpl });
                        writer.confirmDelivery(outbox, level, id);
                        log(`Telegram sent for ${stock.symbol}: ${id}`);
                    } catch (telegramError) { failures += 1; try { const pending = writer.prepareAlert(level, executable, formatAlert({ ...stock, name: stock.name || quote.name }, `${level.side}_TRIGGERED`, executable, now, level, stock.position, quote)); if (telegramError.deliveryUnknown) writer.uncertainDelivery(pending, telegramError); else writer.failDelivery(pending, telegramError); } catch {} error(`Telegram failed for ${stock.symbol}: ${safeError(telegramError)}`); }
                }
            } catch (stockError) { failures += 1; error(`Stock ${stock.symbol} failed: ${safeError(stockError)}`); }
        });
        try { recordEquity(db, now); } catch (snapshotError) { failures += 1; error(`Equity snapshot failed: ${safeError(snapshotError)}`); }
        return failures ? EXIT.PARTIAL : EXIT.OK;
    } finally { release(); }
}

module.exports = { EXIT, parseTradeRepublicMonitorQuote, sendTelegram, formatAlert, formatSdkConnectivityAlert, formatQuoteFeedAlert, checkQuoteAvailability, acquireLock, createWriter, recordEquity, runMonitor, safeError, mapBounded };
