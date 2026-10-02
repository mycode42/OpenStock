'use strict';

const TradesMethods = require('./trades');

class LedgerMethods extends TradesMethods {
    getCapitalEvents() {
        return this.db.prepare('SELECT * FROM capital_events ORDER BY occurred_at ASC, id ASC').all();
    }

    addCapitalEvent(event) {
        return this.db.prepare(`
            INSERT INTO capital_events (event_type, amount_eur, occurred_at, notes)
            VALUES (@event_type, @amount_eur, @occurred_at, @notes)
        `).run(event);
    }

    getEquitySnapshots(since = null) {
        const query = since
            ? 'SELECT * FROM equity_snapshots WHERE snapshot_at >= ? ORDER BY snapshot_at ASC'
            : 'SELECT * FROM equity_snapshots ORDER BY snapshot_at ASC';
        return since ? this.db.prepare(query).all(since) : this.db.prepare(query).all();
    }

    addEquitySnapshot(snapshot) {
        return this.db.prepare(`
            INSERT INTO equity_snapshots (snapshot_at, cash_eur, position_value_eur, equity_eur, net_contributions_eur, financing_eur, source)
            VALUES (@snapshot_at, @cash_eur, @position_value_eur, @equity_eur, @net_contributions_eur, @financing_eur, @source)
            ON CONFLICT(snapshot_at) DO UPDATE SET
                cash_eur=excluded.cash_eur, position_value_eur=excluded.position_value_eur,
                equity_eur=excluded.equity_eur, net_contributions_eur=excluded.net_contributions_eur,
                financing_eur=excluded.financing_eur, source=excluded.source
        `).run(snapshot);
    }

    updateStockTriggerState(id, state) {
        const stmt = this.db.prepare(`
            UPDATE stocks SET trigger_state = ?, updated_at = datetime('now')
            WHERE id = ?
        `);
        return stmt.run(state, id);
    }

    removeStock(id) {
        const stock = this.db.prepare('SELECT id FROM stocks WHERE id = ?').get(id);
        if (!stock) return { changes: 0, archived: false };
        const history = this.db.prepare('SELECT 1 FROM trades WHERE stock_id = ? LIMIT 1').get(id);
        if (history) {
            const result = this.db.prepare(`UPDATE stocks SET archived = 1, enabled = 0, owned = 0,
                trigger_state = 'NONE', updated_at = datetime('now') WHERE id = ?`).run(id);
            this.db.prepare('UPDATE alert_levels SET enabled = 0, active = 0 WHERE stock_id = ?').run(id);
            return { changes: result.changes, archived: true };
        }
        if (this.db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name='broker_instrument_mappings'").get()) this.db.prepare('DELETE FROM broker_instrument_mappings WHERE stock_id = ?').run(id);
        const result = this.db.prepare('DELETE FROM stocks WHERE id = ?').run(id);
        return { changes: result.changes, archived: false };
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

    addAlertForLevel(level, priceEur, messageId) {
        return this.db.prepare('INSERT INTO alerts_log (stock_id, alert_level_id, alert_type, price_eur, threshold_eur, telegram_message_id) VALUES (?, ?, ?, ?, ?, ?)').run(level.stock_id, level.id, level.side, priceEur, level.price_eur, String(messageId));
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

module.exports = LedgerMethods;
