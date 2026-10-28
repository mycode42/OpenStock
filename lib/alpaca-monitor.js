'use strict';

const { EXIT, acquireLock, sendTelegram, safeError } = require('./monitor');
const { makeQuote } = require('./brokers/alpaca');

function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character]));
}

function formatUsd(value) {
    return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 6 }).format(Number(value));
}

function alertMessage(stock, level, executable, quote, position, now) {
    const move = level.trigger_mode === 'PERCENT';
    const name = escapeHtml(stock.name || stock.symbol);
    const sideName = level.side === 'BUY' ? (move ? 'DROP' : 'BUY') : (move ? 'GAIN' : 'SELL');
    const target = formatUsd(level.price_usd);
    const trigger = move && Number(level.change_pct) > 0
        ? `${Number(level.change_pct).toFixed(2)}% from ${formatUsd(level.reference_price_usd)}`
        : target;
    const executableName = level.side === 'BUY' ? 'ask' : 'bid';
    const quantity = Number(position?.qty || 0);
    return `<b>${sideName} · ${name} (${escapeHtml(stock.symbol)}) · ${trigger}</b>\nAlpaca ${String(quote.feed).toUpperCase()} ${executableName}: ${formatUsd(executable)} · midpoint: ${formatUsd(quote.price_usd)}\nPosition: ${quantity} shares · Alpaca\nTime: ${now.toISOString().slice(0, 16).replace('T', ' ')} UTC`;
}

function createWriter(db) {
    const watchlist = db.prepare(`SELECT id, symbol, name FROM alpaca_watchlist
        WHERE enabled=1 AND archived=0 ORDER BY symbol`);
    const levels = db.prepare(`SELECT * FROM alpaca_alert_levels
        WHERE watchlist_id=? AND enabled=1 AND active=1 AND triggered=0 AND retired=0 ORDER BY side, price_usd, id`);
    const trigger = db.prepare(`UPDATE alpaca_alert_levels SET enabled=0, active=0, triggered=1, retired=1,
        triggered_at=?, retired_at=?, updated_at=? WHERE id=? AND enabled=1 AND active=1 AND triggered=0 AND retired=0`);
    const createOutbox = db.prepare(`INSERT OR IGNORE INTO alpaca_alert_outbox
        (alpaca_alert_level_id, delivery_key, symbol, message_html, price_usd, status)
        VALUES (?, ?, ?, ?, ?, 'PENDING')`);
    const getOutbox = db.prepare('SELECT * FROM alpaca_alert_outbox WHERE alpaca_alert_level_id=?');
    const deliveryQueue = db.prepare("SELECT * FROM alpaca_alert_outbox WHERE status IN ('PENDING','FAILED') ORDER BY created_at, id");
    const startDelivery = db.prepare("UPDATE alpaca_alert_outbox SET status='SENDING',attempt_count=attempt_count+1,last_attempt_at=?,last_error=NULL,updated_at=? WHERE id=? AND status IN ('PENDING','FAILED')");
    const sent = db.prepare("UPDATE alpaca_alert_outbox SET status='SENT',telegram_message_id=?,sent_at=?,updated_at=?,last_error=NULL WHERE id=? AND status='SENDING'");
    const failed = db.prepare("UPDATE alpaca_alert_outbox SET status='FAILED',last_error=?,updated_at=? WHERE id=? AND status='SENDING'");
    const uncertain = db.prepare("UPDATE alpaca_alert_outbox SET last_error=?,updated_at=? WHERE id=? AND status='SENDING'");
    return {
        stocks: () => watchlist.all().map(stock => ({ ...stock, levels: levels.all(stock.id) })),
        positionsBySymbol: positions => new Map((positions || []).map(row => [String(row.symbol).toUpperCase(), row])),
        trigger: (stock, level, executable, message, now) => db.transaction(() => {
            const stamp = now.toISOString();
            if (trigger.run(stamp, stamp, stamp, level.id).changes !== 1) return null;
            createOutbox.run(level.id, `alpaca-level:${level.id}`, stock.symbol, message, executable);
            return getOutbox.get(level.id);
        }).immediate(),
        deliveryQueue: () => deliveryQueue.all(),
        startDelivery: (item, now) => startDelivery.run(now.toISOString(), now.toISOString(), item.id).changes === 1,
        confirmDelivery: (item, messageId, now) => sent.run(String(messageId), now.toISOString(), now.toISOString(), item.id),
        failDelivery: (item, error, now) => failed.run(safeError(error), now.toISOString(), item.id),
        uncertainDelivery: (item, error, now) => uncertain.run(`Delivery outcome unknown: ${safeError(error)}`, now.toISOString(), item.id)
    };
}

async function runAlpacaMonitor({ db, alpacaClient, log = () => {}, error = () => {}, config = {}, now = () => new Date(), fetchImpl = globalThis.fetch } = {}) {
    const release = acquireLock(config.lockPath);
    if (!release) return EXIT.LOCKED;
    const writer = createWriter(db);
    let failures = 0;
    try {
        const stocks = writer.stocks();
        if (!alpacaClient?.isConfigured?.()) {
            error('Alpaca credentials are not configured; price monitoring was skipped');
            return EXIT.CONFIG;
        }
        if (stocks.length) {
            try {
                const [quotePayload, positions] = await Promise.all([
                    alpacaClient.getLatestQuotes(stocks.map(stock => stock.symbol)),
                    alpacaClient.getPositions()
                ]);
                const positionsBySymbol = writer.positionsBySymbol(positions);
                const timestamp = now();
                for (const stock of stocks) {
                    try {
                        const quote = makeQuote(quotePayload.quotes[stock.symbol]);
                        if (!quote || !Number.isFinite(quote.price_usd) || quote.price_usd <= 0) continue;
                        quote.feed = quotePayload.feed;
                        const position = positionsBySymbol.get(stock.symbol) || null;
                        const quantity = Number(position?.qty || 0);
                        log(`${stock.symbol} [Alpaca ${quotePayload.feed}]: ${formatUsd(quote.price_usd)} (bid=${quote.bid_usd ?? 'n/a'} ask=${quote.ask_usd ?? 'n/a'}, position=${quantity})`);
                        for (const level of stock.levels) {
                            if (level.side === 'SELL' && Math.abs(quantity) < 1e-12) continue;
                            const executable = level.side === 'BUY' ? (quote.ask_usd ?? quote.price_usd) : (quote.bid_usd ?? quote.price_usd);
                            if (!Number.isFinite(executable) || executable <= 0) continue;
                            const hit = level.side === 'BUY' ? executable <= level.price_usd : executable >= level.price_usd;
                            if (!hit) continue;
                            try {
                                const message = alertMessage(stock, level, executable, quote, position, timestamp);
                                const outbox = writer.trigger(stock, level, executable, message, timestamp);
                                if (outbox) log(`Alpaca alert queued for ${stock.symbol} at ${formatUsd(executable)}`);
                            } catch (triggerError) {
                                failures += 1;
                                error(`Could not record Alpaca alert for ${stock.symbol}: ${safeError(triggerError)}`);
                            }
                        }
                    } catch (stockError) {
                        failures += 1;
                        error(`Alpaca monitor failed for ${stock.symbol}: ${safeError(stockError)}`);
                    }
                }
            } catch (quoteError) {
                failures += 1;
                error(`Alpaca quote scan failed: ${safeError(quoteError)}`);
            }
        }

        for (const item of writer.deliveryQueue()) {
            if (!writer.startDelivery(item, now())) continue;
            try {
                const messageId = await sendTelegram(item.message_html, { ...config.telegram, fetchImpl, timeoutMs: config.timeoutMs });
                writer.confirmDelivery(item, messageId, now());
                log(`Alpaca Telegram alert sent for ${item.symbol}: ${messageId}`);
            } catch (deliveryError) {
                failures += 1;
                if (deliveryError.deliveryUnknown) writer.uncertainDelivery(item, deliveryError, now());
                else writer.failDelivery(item, deliveryError, now());
                error(`Alpaca Telegram alert failed for ${item.symbol}: ${safeError(deliveryError)}`);
            }
        }
        return failures ? EXIT.PARTIAL : EXIT.OK;
    } finally {
        release();
    }
}

module.exports = { runAlpacaMonitor, createWriter, alertMessage };
