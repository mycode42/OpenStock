'use strict';

const StocksMethods = require('./stocks');

class QuotesMethods extends StocksMethods {
    updateStockQuote(id, { bidEur, askEur, midpointEur, quoteTime = null }) {
        const recordedAt = quoteTime ? String(quoteTime).replace('T', ' ').replace(/\.\d{3}Z$/, '').replace(/Z$/, '') : null;
        return this.db.prepare(`UPDATE stocks SET current_price_usd=NULL, current_price_eur=?, current_bid_eur=?, current_ask_eur=?, current_midpoint_eur=?, last_exchange_rate=NULL, last_price_update=COALESCE(?, datetime('now')), updated_at=datetime('now') WHERE id=?`).run(midpointEur, bidEur, askEur, midpointEur, recordedAt, id);
    }

    persistQuote(stock, quote, { recordedAt = null } = {}) {
        if (!String(quote.source || '').startsWith('trade_republic')) throw new Error('Unsupported quote source');
        return this.db.transaction(() => {
            this.updateStockQuote(stock.id, { bidEur: quote.bidEur ?? null, askEur: quote.askEur ?? null, midpointEur: quote.priceEur, quoteTime: quote.quoteTime });
            this.addPriceSnapshot(stock.id, null, quote.priceEur, null, quote.source, quote.quoteTime, recordedAt);
            if (quote.name && (!stock.name || stock.name === '')) this.updateStock(stock.id, { name: quote.name });
            if (stock.mapping) this.addBrokerQuote({ broker: 'trade_republic', isin: stock.mapping.isin, exchange: stock.mapping.exchange, bid_eur: quote.bidEur ?? null, ask_eur: quote.askEur ?? null, last_eur: quote.lastEur ?? null, currency: 'EUR', quote_time: quote.quoteTime, observed_at: quote.observedAt || new Date().toISOString(), source: quote.source });
        })();
    }

    addPriceSnapshot(stockId, priceUsd, priceEur, exchangeRate, source, quoteTime = null, recordedAt = null) {
        const stmt = this.db.prepare(`
            INSERT INTO price_history (
                stock_id, price_usd, price_eur, exchange_rate, source, quote_time, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))
            ON CONFLICT(stock_id, recorded_at) DO UPDATE SET
                price_usd = excluded.price_usd,
                price_eur = excluded.price_eur,
                exchange_rate = excluded.exchange_rate,
                source = excluded.source,
                quote_time = excluded.quote_time
        `);
        return stmt.run(stockId, priceUsd, priceEur, exchangeRate, source, quoteTime, recordedAt);
    }

    hasDailyBackfill(stockId) { return this.hasTradeRepublicHistory(stockId); }
    hasTradeRepublicHistory(stockId) { return Boolean(this.db.prepare("SELECT 1 FROM price_history WHERE stock_id = ? AND source = 'trade_republic_history' LIMIT 1").get(stockId)); }
    hasDenseTradeRepublicHistory(stockId, minimumRecentPoints = 20) {
        const row = this.db.prepare(`SELECT COUNT(*) AS count FROM price_history
            WHERE stock_id = ? AND source = 'trade_republic_history' AND recorded_at >= datetime('now', '-35 days')`).get(stockId);
        return Number(row?.count || 0) >= minimumRecentPoints;
    }
    hasCompletedHistoryBackfill(stockId, source, ranges) {
        if (!this.db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name='history_backfill_runs'").get()) return false;
        const expected = Array.isArray(ranges) ? ranges : [];
        if (!expected.length) return false;
        const rows = this.db.prepare(`SELECT range FROM history_backfill_runs WHERE stock_id=? AND source=? AND status='SUCCEEDED'`).all(stockId, source);
        const done = new Set(rows.map(row => row.range));
        return expected.every(range => done.has(range));
    }
    startHistoryBackfill(stockId, source, range) { return this.db.prepare(`INSERT INTO history_backfill_runs(stock_id,source,range,status,started_at,completed_at,last_error) VALUES (?,?,?,'RUNNING',datetime('now'),NULL,NULL) ON CONFLICT(stock_id,source,range) DO UPDATE SET status='RUNNING',started_at=datetime('now'),completed_at=NULL,last_error=NULL`).run(stockId, source, range); }
    completeHistoryBackfill(stockId, source, range, pointsSaved) { return this.db.prepare(`UPDATE history_backfill_runs SET status='SUCCEEDED',points_saved=?,completed_at=datetime('now'),last_error=NULL WHERE stock_id=? AND source=? AND range=?`).run(pointsSaved, stockId, source, range); }
    failHistoryBackfill(stockId, source, range, error) { return this.db.prepare(`INSERT INTO history_backfill_runs(stock_id,source,range,status,last_error,completed_at) VALUES (?,?,?,'FAILED',?,datetime('now')) ON CONFLICT(stock_id,source,range) DO UPDATE SET status='FAILED',last_error=excluded.last_error,completed_at=excluded.completed_at`).run(stockId, source, range, String(error || 'unknown error').slice(0, 300)); }

    addHistoricalPriceSnapshots(stockId, points, source = 'trade_republic_history') {
        const insert = this.db.prepare(`
            INSERT INTO price_history (stock_id, price_usd, price_eur, exchange_rate, source, quote_time, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(stock_id, recorded_at) DO UPDATE SET price_usd=excluded.price_usd, price_eur=excluded.price_eur,
              exchange_rate=excluded.exchange_rate, source=excluded.source, quote_time=excluded.quote_time
        `);
        return this.db.transaction(rows => rows.reduce((saved, point) => saved + insert.run(stockId, point.priceUsd ?? null, point.priceEur ?? null, point.exchangeRate ?? null, point.source || source, point.quoteTime || point.recordedAt, point.recordedAt).changes, 0))(Array.isArray(points) ? points : []);
    }

    getPriceHistory(stockId, since, bucketSeconds, { sources = null, eurOnly = false } = {}) {
        const selectedSources = Array.isArray(sources) ? sources.filter(Boolean) : [];
        const sourceFilter = selectedSources.length ? ` AND source IN (${selectedSources.map(() => '?').join(', ')})` : '';
        const eurFilter = eurOnly ? ' AND price_eur IS NOT NULL' : '';
        return this.db.prepare(`
            WITH eligible AS (
                SELECT id, COALESCE(NULLIF(quote_time, ''), recorded_at) AS chart_at
                FROM price_history
                WHERE stock_id = ?
                  AND datetime(COALESCE(NULLIF(quote_time, ''), recorded_at)) >= datetime('now', ?)${sourceFilter}${eurFilter}
            ), sampled AS (
                SELECT MAX(id) AS id
                FROM eligible
                GROUP BY CAST(CAST(strftime('%s', chart_at) AS INTEGER) / ? AS INTEGER)
            )
            SELECT eligible.chart_at AS recorded_at, price_history.recorded_at AS observed_at,
                   price_history.quote_time, price_history.price_usd, price_history.price_eur, price_history.source
            FROM sampled
            JOIN eligible ON eligible.id = sampled.id
            JOIN price_history ON price_history.id = sampled.id
            ORDER BY CAST(strftime('%s', eligible.chart_at) AS INTEGER) ASC, eligible.id ASC
        `).all(stockId, since, ...selectedSources, bucketSeconds);
    }

    // ===== TRADES =====
}

module.exports = QuotesMethods;
