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
                   buy_threshold_eur, sell_threshold_eur, owned, enabled, archived,
                   last_price_update, last_exchange_rate, trigger_state,
                   created_at, updated_at
            FROM stocks
            ORDER BY symbol ASC
        `).all();
    }

    getConfigStocks() {
        return this.db.prepare('SELECT * FROM stocks WHERE archived = 0 ORDER BY symbol ASC').all();
    }

    getActiveStocks() {
        return this.db.prepare('SELECT * FROM stocks WHERE archived = 0 AND enabled = 1 ORDER BY symbol ASC').all();
    }

    getJournalStocks() {
        return this.db.prepare(`
            SELECT s.*, EXISTS(SELECT 1 FROM trades t WHERE t.stock_id = s.id) AS has_journal_history
            FROM stocks s WHERE s.archived = 0 OR EXISTS(SELECT 1 FROM trades t WHERE t.stock_id = s.id)
            ORDER BY s.symbol ASC
        `).all();
    }

    getEnabledStocks() {
        return this.db.prepare(`
            SELECT id, symbol, name, currency, buy_threshold_eur, sell_threshold_eur,
                   enabled, archived, trigger_state
            FROM stocks
            WHERE enabled = 1 AND archived = 0
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

    restoreArchivedStock(id, { name, currency, buyThreshold, sellThreshold }) {
        return this.db.prepare(`
            UPDATE stocks SET name = ?, currency = ?, buy_threshold_eur = ?, sell_threshold_eur = ?,
                archived = 0, enabled = 1, owned = 0, trigger_state = 'NONE', updated_at = datetime('now')
            WHERE id = ? AND archived = 1
        `).run(name, currency, buyThreshold, sellThreshold, id);
    }

    updateStock(id, updates) {
        const allowedFields = ['buy_threshold_eur', 'sell_threshold_eur', 'owned', 'enabled', 'name', 'archived'];
        const setClauses = [];
        const values = [];

        for (const [key, value] of Object.entries(updates)) {
            if (allowedFields.includes(key)) {
                setClauses.push(`${key} = ?`);
                values.push(value);
            }
        }

        // A sold stock must not retain an active buy/sell alert state. Keep
        // collecting its prices/history when enabled, but disarm triggers as
        // soon as ownership is changed to sold.
        if (Object.prototype.hasOwnProperty.call(updates, 'owned') && !Number(updates.owned)) {
            setClauses.push("trigger_state = 'NONE'");
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

    hasDailyBackfill(stockId) {
        return Boolean(this.db.prepare("SELECT 1 FROM price_history WHERE stock_id = ? AND source = 'yahoo_backfill' LIMIT 1").get(stockId));
    }

    addHistoricalPriceSnapshots(stockId, points) {
        const insert = this.db.prepare(`
            INSERT INTO price_history (stock_id, price_usd, source, quote_time, recorded_at)
            VALUES (?, ?, 'yahoo_backfill', ?, ?)
            ON CONFLICT(stock_id, recorded_at) DO NOTHING
        `);
        return this.db.transaction(rows => rows.reduce((saved, point) => saved + insert.run(stockId, point.priceUsd, point.recordedAt, point.recordedAt).changes, 0))(Array.isArray(points) ? points : []);
    }

    getPriceHistory(stockId, since, bucketSeconds) {
        return this.db.prepare(`
            WITH sampled AS (
                SELECT MAX(id) AS id
                FROM price_history
                WHERE stock_id = ? AND recorded_at >= datetime('now', ?)
                GROUP BY CAST(CAST(strftime('%s', recorded_at) AS INTEGER) / ? AS INTEGER)
            )
            SELECT recorded_at, quote_time, price_usd, price_eur, source
            FROM price_history
            WHERE id IN (SELECT id FROM sampled)
            ORDER BY recorded_at ASC
        `).all(stockId, since, bucketSeconds);
    }

    // ===== TRADES =====
    getAllTrades(filters = {}) {
        const clauses = [];
        const values = [];
        if (filters.status === 'open') clauses.push('t.closed_at IS NULL');
        if (filters.status === 'closed') clauses.push('t.closed_at IS NOT NULL');
        if (filters.symbol) { clauses.push('s.symbol = ?'); values.push(filters.symbol); }
        const where = clauses.length ? `WHERE ${clauses.join(' AND ')}` : '';
        return this.db.prepare(`
            SELECT t.*, s.symbol, s.name, s.current_price_eur
            FROM trades t JOIN stocks s ON s.id = t.stock_id
            ${where}
            ORDER BY COALESCE(t.closed_at, t.opened_at) DESC, t.id DESC
        `).all(...values);
    }

    getTradeById(id) {
        return this.db.prepare(`
            SELECT t.*, s.symbol, s.name, s.current_price_eur
            FROM trades t JOIN stocks s ON s.id = t.stock_id
            WHERE t.id = ?
        `).get(id);
    }

    addTrade(trade) {
        return this.db.prepare(`
            INSERT INTO trades (
                trade_id, stock_id, opened_at, closed_at, quantity, currency,
                entry_price, entry_fx_rate, entry_price_eur, entry_fee_eur,
                exit_price, exit_fx_rate, exit_price_eur, exit_fee_eur, dividend_eur, strategy, notes
            ) VALUES (
                @trade_id, @stock_id, @opened_at, @closed_at, @quantity, @currency,
                @entry_price, @entry_fx_rate, @entry_price_eur, @entry_fee_eur,
                @exit_price, @exit_fx_rate, @exit_price_eur, @exit_fee_eur, @dividend_eur, @strategy, @notes
            )
        `).run(trade);
    }

    updateTrade(id, trade) {
        return this.db.prepare(`
            UPDATE trades SET
                stock_id=@stock_id, opened_at=@opened_at, closed_at=@closed_at, quantity=@quantity, currency=@currency,
                entry_price=@entry_price, entry_fx_rate=@entry_fx_rate, entry_price_eur=@entry_price_eur, entry_fee_eur=@entry_fee_eur,
                exit_price=@exit_price, exit_fx_rate=@exit_fx_rate, exit_price_eur=@exit_price_eur, exit_fee_eur=@exit_fee_eur, dividend_eur=@dividend_eur,
                strategy=@strategy, notes=@notes, updated_at=datetime('now')
            WHERE id=@id
        `).run({ ...trade, id });
    }

    // ===== CAPITAL & EQUITY =====
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
            return { changes: result.changes, archived: true };
        }
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
