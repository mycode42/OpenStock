const Database = require('better-sqlite3');

class OpenStockDB {
    constructor(dbPath) {
        this.db = new Database(dbPath);
        this.db.pragma('journal_mode = WAL');
        this.db.pragma('synchronous = NORMAL');
        this.db.pragma('foreign_keys = ON');
    }

    // ===== STOCKS =====
    getAllStocks() {
        return this.db.prepare(`
            SELECT id, symbol, name, currency, current_price_usd, current_price_eur,
                   buy_threshold_eur, sell_threshold_eur, owned, enabled,
                   last_price_update, last_exchange_rate, trigger_state,
                   created_at, updated_at
            FROM stocks
            ORDER BY symbol ASC
        `).all();
    }

    getEnabledStocks() {
        return this.db.prepare(`
            SELECT id, symbol, name, currency, buy_threshold_eur, sell_threshold_eur,
                   enabled, trigger_state
            FROM stocks
            WHERE enabled = 1
            ORDER BY symbol ASC
        `).all();
    }

    getStockById(id) {
        return this.db.prepare('SELECT * FROM stocks WHERE id = ?').get(id);
    }

    getStockBySymbol(symbol) {
        return this.db.prepare('SELECT * FROM stocks WHERE symbol = ?').get(symbol);
    }

    addStock(symbol, name, currency, buyThreshold, sellThreshold) {
        const stmt = this.db.prepare(`
            INSERT INTO stocks (symbol, name, currency, buy_threshold_eur, sell_threshold_eur)
            VALUES (?, ?, ?, ?, ?)
        `);
        return stmt.run(symbol, name, currency, buyThreshold, sellThreshold);
    }

    updateStock(id, updates) {
        const allowedFields = ['buy_threshold_eur', 'sell_threshold_eur', 'owned', 'enabled', 'name'];
        const setClauses = [];
        const values = [];

        for (const [key, value] of Object.entries(updates)) {
            if (allowedFields.includes(key)) {
                setClauses.push(`${key} = ?`);
                values.push(value);
            }
        }

        if (setClauses.length === 0) {
            throw new Error('No valid fields to update');
        }

        values.push(id);
        const stmt = this.db.prepare(`
            UPDATE stocks SET ${setClauses.join(', ')}, updated_at = datetime('now')
            WHERE id = ?
        `);
        return stmt.run(...values);
    }

    updateStockPrice(id, priceUsd, priceEur, exchangeRate) {
        const stmt = this.db.prepare(`
            UPDATE stocks
            SET current_price_usd = ?, current_price_eur = ?, last_price_update = datetime('now'),
                last_exchange_rate = ?, updated_at = datetime('now')
            WHERE id = ?
        `);
        return stmt.run(priceUsd, priceEur, exchangeRate, id);
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

    getPriceHistory(stockId, since, bucketSeconds) {
        return this.db.prepare(`
            WITH sampled AS (
                SELECT MAX(id) AS id
                FROM price_history
                WHERE stock_id = ? AND recorded_at >= datetime('now', ?)
                GROUP BY CAST(strftime('%s', recorded_at) AS INTEGER) / ?
            )
            SELECT recorded_at, quote_time, price_usd, price_eur, source
            FROM price_history
            WHERE id IN (SELECT id FROM sampled)
            ORDER BY recorded_at ASC
        `).all(stockId, since, bucketSeconds);
    }

    updateStockTriggerState(id, state) {
        const stmt = this.db.prepare(`
            UPDATE stocks SET trigger_state = ?, updated_at = datetime('now')
            WHERE id = ?
        `);
        return stmt.run(state, id);
    }

    removeStock(id) {
        const stmt = this.db.prepare('DELETE FROM stocks WHERE id = ?');
        return stmt.run(id);
    }

    // ===== ALERTS LOG =====
    getLatestAlert(stockId) {
        return this.db.prepare(`
            SELECT * FROM alerts_log
            WHERE stock_id = ?
            ORDER BY sent_at DESC
            LIMIT 1
        `).get(stockId);
    }

    addAlert(stockId, alertType, priceEur, thresholdEur) {
        const stmt = this.db.prepare(`
            INSERT INTO alerts_log (stock_id, alert_type, price_eur, threshold_eur)
            VALUES (?, ?, ?, ?)
        `);
        return stmt.run(stockId, alertType, priceEur, thresholdEur);
    }

    // ===== EXCHANGE RATES =====
    getLatestExchangeRate() {
        return this.db.prepare('SELECT * FROM exchange_rates ORDER BY fetched_at DESC LIMIT 1').get();
    }

    addExchangeRate(rate) {
        const stmt = this.db.prepare(`
            INSERT INTO exchange_rates (rate) VALUES (?)
        `);
        return stmt.run(rate);
    }
}

module.exports = OpenStockDB;
