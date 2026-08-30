const Database = require('better-sqlite3');
const path = require('path');

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
                   last_price_update, trigger_state
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

    // ===== JOURNAL =====
    getAllJournalEntries() {
        return this.db.prepare(`
            SELECT j.*, s.symbol
            FROM journal_entries j
            JOIN stocks s ON j.stock_id = s.id
            ORDER BY j.created_at DESC
        `).all();
    }

    getJournalEntriesByStock(stockId) {
        return this.db.prepare(`
            SELECT j.*, s.symbol
            FROM journal_entries j
            JOIN stocks s ON j.stock_id = s.id
            WHERE j.stock_id = ?
            ORDER BY j.created_at DESC
        `).all(stockId);
    }

    addJournalEntry(stockId, action, priceEur, quantity, notes) {
        const stmt = this.db.prepare(`
            INSERT INTO journal_entries (stock_id, action, price_eur, quantity, notes)
            VALUES (?, ?, ?, ?, ?)
        `);
        return stmt.run(stockId, action, priceEur, quantity, notes);
    }

    updateJournalEntry(id, action, priceEur, quantity, notes) {
        const stmt = this.db.prepare(`
            UPDATE journal_entries
            SET action = ?, price_eur = ?, quantity = ?, notes = ?,
                updated_at = datetime('now')
            WHERE id = ?
        `);
        return stmt.run(action, priceEur, quantity, notes, id);
    }

    deleteJournalEntry(id) {
        const stmt = this.db.prepare('DELETE FROM journal_entries WHERE id = ?');
        return stmt.run(id);
    }
}

module.exports = OpenStockDB;