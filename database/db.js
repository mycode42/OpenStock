const Database = require('better-sqlite3');
const crypto = require('node:crypto');

function executionKey(broker, identity) {
    return crypto.createHash('sha256').update(`${broker}:${identity}`).digest('hex');
}

class OpenStockDB {
    constructor(dbPath) {
        this.db = new Database(dbPath);
        this.db.pragma('journal_mode = WAL');
        this.db.pragma('synchronous = NORMAL');
        this.db.pragma('foreign_keys = ON');
    }

    // ===== STOCKS =====
    getAllStocks() {
        return this.db.prepare('SELECT * FROM stocks ORDER BY symbol ASC').all();
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

    getArchivedStocks() {
        return this.db.prepare(`
            SELECT s.*, EXISTS(SELECT 1 FROM trades t WHERE t.stock_id = s.id) AS has_journal_history
            FROM stocks s WHERE s.archived = 1 ORDER BY s.symbol ASC
        `).all();
    }

    getEnabledStocks() {
        return this.db.prepare(`
            SELECT id, symbol, name, isin, currency, buy_threshold_eur, sell_threshold_eur,
                   enabled, archived, trigger_state, price_source
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

    getLatestPriceObservation(stockId) {
        return this.db.prepare('SELECT source, quote_time, recorded_at FROM price_history WHERE stock_id = ? ORDER BY recorded_at DESC, id DESC LIMIT 1').get(stockId) || null;
    }

    getPositionSummary(stockId) {
        const open = this.db.prepare('SELECT COALESCE(SUM(quantity), 0) AS quantity FROM trades WHERE stock_id = ? AND closed_at IS NULL').get(stockId);
        const quantity = Number(open.quantity || 0);
        return {
            quantity,
            owned: quantity > 0,
            source: 'journal',
            warning: quantity > 0 ? null : 'No open Journal position; SELL alerts are suppressed.'
        };
    }

    getAlertLevels(stockId) { return this.db.prepare('SELECT al.*, s.symbol FROM alert_levels al JOIN stocks s ON s.id = al.stock_id WHERE al.stock_id = ? ORDER BY al.side, al.price_eur, al.id').all(stockId); }
    getActiveAlertLevels(stockId) { return this.db.prepare('SELECT * FROM alert_levels WHERE stock_id = ? AND enabled = 1 AND active = 1 AND triggered = 0 AND retired = 0 ORDER BY side, price_eur, id').all(stockId); }
    getAlertLevelById(id) { return this.db.prepare('SELECT al.*, s.symbol FROM alert_levels al JOIN stocks s ON s.id = al.stock_id WHERE al.id = ?').get(id); }
    addAlertLevel(stockId, side, priceEur) {
        if (!this.getStockById(stockId)) throw new Error('Stock not found');
        if (!['BUY', 'SELL'].includes(String(side).toUpperCase())) throw new Error('Side must be BUY or SELL');
        if (!Number.isFinite(Number(priceEur)) || Number(priceEur) <= 0) throw new Error('Alert price must be positive');
        const result = this.db.prepare('INSERT INTO alert_levels (stock_id, side, price_eur) VALUES (?, ?, ?)').run(stockId, String(side).toUpperCase(), Number(priceEur)); return this.getAlertLevelById(result.lastInsertRowid);
    }
    updateAlertLevel(id, updates) {
        const existing = this.getAlertLevelById(id);
        if (!existing) throw new Error('Alert level not found');
        if (updates.side !== undefined && !['BUY', 'SELL'].includes(String(updates.side).toUpperCase())) throw new Error('Side must be BUY or SELL');
        if (updates.price_eur !== undefined && (!Number.isFinite(Number(updates.price_eur)) || Number(updates.price_eur) <= 0)) throw new Error('Alert price must be positive');
        for (const key of ['enabled', 'active']) if (updates[key] !== undefined && ![0, 1, true, false].includes(updates[key])) throw new Error(`${key} must be boolean`);
        if ((existing.triggered || existing.retired) && (updates.enabled === 1 || updates.enabled === true || updates.active === 1 || updates.active === true)) throw new Error('Retired alert levels must be rearmed explicitly');

        const normalized = {};
        if (updates.side !== undefined) normalized.side = String(updates.side).toUpperCase();
        if (updates.price_eur !== undefined) normalized.price_eur = Number(updates.price_eur);
        if (updates.enabled !== undefined) normalized.enabled = updates.enabled ? 1 : 0;
        if (updates.active !== undefined) normalized.active = updates.active ? 1 : 0;
        if (normalized.enabled === 0) normalized.active = 0;
        if (normalized.enabled === 1 && updates.active === undefined) normalized.active = 1;
        const effectiveEnabled = normalized.enabled ?? Number(existing.enabled);
        if (normalized.active === 1 && effectiveEnabled !== 1) throw new Error('An active alert level must be enabled');

        const entries = Object.entries(normalized);
        if (!entries.length) throw new Error('No valid alert level fields to update');
        const result = this.db.prepare(`UPDATE alert_levels SET ${entries.map(([key]) => `${key} = ?`).join(', ')} WHERE id = ?`).run(...entries.map(([, value]) => value), id);
        if (!result.changes) throw new Error('Alert level not found');
        return this.getAlertLevelById(id);
    }
    deleteAlertLevel(id) { return this.db.prepare('DELETE FROM alert_levels WHERE id = ?').run(id); }
    rearmAlertLevel(id) {
        this.db.transaction(() => {
            // A level's outbox row represents one notification occurrence. Retaining
            // a prior SENT row would let the monitor retire a rearmed level without
            // issuing a fresh Telegram request.
            this.db.prepare('DELETE FROM alert_outbox WHERE alert_level_id=?').run(id);
            this.db.prepare("UPDATE alert_levels SET enabled=1, active=1, triggered=0, retired=0, triggered_at=NULL, retired_at=NULL WHERE id=?").run(id);
        })();
        return this.getAlertLevelById(id);
    }
    retireAlertLevel(id) { return this.db.prepare("UPDATE alert_levels SET enabled=0, active=0, triggered=1, retired=1, triggered_at=datetime('now'), retired_at=datetime('now') WHERE id=?").run(id); }

    addStock(symbol, name, currency, buyThreshold, sellThreshold, isin = null) {
        const stmt = this.db.prepare(`
            INSERT INTO stocks (symbol, name, isin, currency, buy_threshold_eur, sell_threshold_eur)
            VALUES (?, ?, ?, ?, ?, ?)
        `);
        return this.db.transaction(() => {
            const result = stmt.run(symbol, name, isin, currency, buyThreshold, sellThreshold);
            const add = this.db.prepare('INSERT INTO alert_levels (stock_id, side, price_eur) VALUES (?, ?, ?)');
            add.run(result.lastInsertRowid, 'BUY', buyThreshold);
            add.run(result.lastInsertRowid, 'SELL', sellThreshold);
            return result;
        })();
    }

    addStockWithMapping({ symbol, name, isin, currency = 'EUR', buyThreshold, sellThreshold, mapping }) {
        return this.db.transaction(() => {
            const result = this.db.prepare(`INSERT INTO stocks (symbol, name, isin, currency, buy_threshold_eur, sell_threshold_eur, price_source, current_price_usd, last_exchange_rate)
                VALUES (?, ?, ?, ?, ?, ?, 'trade_republic', NULL, NULL)`).run(symbol, name, isin, currency, buyThreshold, sellThreshold);
            const stockId = Number(result.lastInsertRowid);
            const add = this.db.prepare('INSERT INTO alert_levels (stock_id, side, price_eur) VALUES (?, ?, ?)');
            add.run(stockId, 'BUY', buyThreshold); add.run(stockId, 'SELL', sellThreshold);
            this.saveBrokerMapping({ ...mapping, stock_id: stockId });
            return { lastInsertRowid: stockId };
        })();
    }

    // Alert levels are canonical for monitoring. Legacy threshold columns are
    // retained as compatibility metadata for direct database callers only.
    restoreArchivedStock(id, { name, currency, buyThreshold = null, sellThreshold = null }) {
        const result = this.db.prepare(`
            UPDATE stocks SET name = ?, currency = ?, buy_threshold_eur = COALESCE(?, buy_threshold_eur), sell_threshold_eur = COALESCE(?, sell_threshold_eur),
                archived = 0, enabled = 1, owned = 0, trigger_state = 'NONE', updated_at = datetime('now')
            WHERE id = ? AND archived = 1
        `).run(name, currency, buyThreshold, sellThreshold, id);
        if (result.changes) this.db.prepare('UPDATE alert_levels SET enabled = CASE WHEN retired = 0 THEN 1 ELSE 0 END, active = CASE WHEN retired = 0 THEN 1 ELSE 0 END WHERE stock_id = ?').run(id);
        return result;
    }

    updateStock(id, updates) {
        const allowedFields = ['buy_threshold_eur', 'sell_threshold_eur', 'enabled', 'name', 'archived', 'price_source'];
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

    updateStockQuote(id, { bidEur, askEur, midpointEur, quoteTime = null }) {
        const recordedAt = quoteTime ? String(quoteTime).replace('T', ' ').replace(/\.\d{3}Z$/, '').replace(/Z$/, '') : null;
        return this.db.prepare(`UPDATE stocks SET current_price_usd=NULL, current_price_eur=?, current_bid_eur=?, current_ask_eur=?, current_midpoint_eur=?, last_exchange_rate=NULL, last_price_update=COALESCE(?, datetime('now')), updated_at=datetime('now') WHERE id=?`).run(midpointEur, bidEur, askEur, midpointEur, recordedAt, id);
    }

    persistQuote(stock, quote, { recordedAt = null } = {}) {
        const isBrokerQuote = /^(trade_republic|pytr_)/.test(String(quote.source || ''));
        return this.db.transaction(() => {
            if (isBrokerQuote) this.updateStockQuote(stock.id, { bidEur: quote.bidEur ?? null, askEur: quote.askEur ?? null, midpointEur: quote.priceEur, quoteTime: quote.quoteTime });
            else this.updateStockPrice(stock.id, quote.priceUsd, quote.priceEur, quote.exchangeRate);
            this.addPriceSnapshot(stock.id, quote.priceUsd ?? null, quote.priceEur, isBrokerQuote ? null : (quote.exchangeRate ?? null), quote.source, quote.quoteTime, recordedAt);
            if (quote.name && (!stock.name || stock.name === '')) this.updateStock(stock.id, { name: quote.name });
            if (isBrokerQuote && stock.mapping) this.addBrokerQuote({ broker: 'trade_republic', isin: stock.mapping.isin, exchange: stock.mapping.exchange, bid_eur: quote.bidEur ?? null, ask_eur: quote.askEur ?? null, last_eur: quote.lastEur ?? null, currency: 'EUR', quote_time: quote.quoteTime, observed_at: quote.observedAt || new Date().toISOString(), source: quote.source });
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

    hasDailyBackfill(stockId) {
        return Boolean(this.db.prepare("SELECT 1 FROM price_history WHERE stock_id = ? AND source IN ('yahoo_backfill', 'trade_republic_history') LIMIT 1").get(stockId));
    }
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

    addHistoricalPriceSnapshots(stockId, points, source = 'yahoo_backfill') {
        const insert = this.db.prepare(`
            INSERT INTO price_history (stock_id, price_usd, price_eur, exchange_rate, source, quote_time, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(stock_id, recorded_at) DO UPDATE SET price_usd=excluded.price_usd, price_eur=excluded.price_eur,
              exchange_rate=excluded.exchange_rate, source=excluded.source, quote_time=excluded.quote_time
        `);
        if (source !== 'trade_republic_history') {
            // Yahoo backfill never replaces an existing live observation.
            const legacyInsert = this.db.prepare(`INSERT INTO price_history (stock_id, price_usd, price_eur, exchange_rate, source, quote_time, recorded_at)
              VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(stock_id, recorded_at) DO NOTHING`);
            return this.db.transaction(rows => rows.reduce((saved, point) => saved + legacyInsert.run(stockId, point.priceUsd ?? null, point.priceEur ?? null, point.exchangeRate ?? null, point.source || source, point.quoteTime || point.recordedAt, point.recordedAt).changes, 0))(Array.isArray(points) ? points : []);
        }
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
    tradeQueryParts(filters = {}) {
        const clauses = [];
        const values = [];
        if (filters.status === 'open') clauses.push('t.closed_at IS NULL');
        if (filters.status === 'closed') clauses.push('t.closed_at IS NOT NULL');
        if (filters.symbol) { clauses.push('s.symbol = ?'); values.push(filters.symbol); }
        const where = clauses.length ? `WHERE ${clauses.join(' AND ')}` : '';
        return { where, values };
    }

    getAllTrades(filters = {}) {
        const { where, values } = this.tradeQueryParts(filters);
        return this.db.prepare(`
            SELECT t.*, s.symbol, s.name, s.current_price_eur
            FROM trades t JOIN stocks s ON s.id = t.stock_id
            ${where}
            ORDER BY COALESCE(t.closed_at, t.opened_at) DESC, t.id DESC
        `).all(...values);
    }

    getTradesPage(filters = {}, { page = 1, pageSize = 20 } = {}) {
        const { where, values } = this.tradeQueryParts(filters);
        const safePageSize = Math.min(100, Math.max(1, Math.floor(Number(pageSize) || 20)));
        const requestedPage = Math.max(1, Math.floor(Number(page) || 1));
        const total = this.db.prepare(`SELECT COUNT(*) AS total FROM trades t JOIN stocks s ON s.id = t.stock_id ${where}`).get(...values).total;
        const totalPages = Math.max(1, Math.ceil(total / safePageSize));
        const safePage = Math.min(requestedPage, totalPages);
        const items = this.db.prepare(`
            SELECT t.*, s.symbol, s.name, s.current_price_eur
            FROM trades t JOIN stocks s ON s.id = t.stock_id
            ${where}
            ORDER BY COALESCE(t.closed_at, t.opened_at) DESC, t.id DESC
            LIMIT ? OFFSET ?
        `).all(...values, safePageSize, (safePage - 1) * safePageSize);
        return { items, page: safePage, page_size: safePageSize, total, total_pages: totalPages };
    }

    getTradeById(id) {
        return this.db.prepare(`
            SELECT t.*, s.symbol, s.name, s.current_price_eur
            FROM trades t JOIN stocks s ON s.id = t.stock_id
            WHERE t.id = ?
        `).get(id);
    }

    addTrade(trade) {
        const insert = this.db.prepare(`
            INSERT INTO trades (
                trade_id, stock_id, opened_at, closed_at, quantity, currency,
                entry_price, entry_fx_rate, entry_price_eur, entry_fee_eur,
                exit_price, exit_fx_rate, exit_price_eur, exit_fee_eur, dividend_eur, strategy, notes
            ) VALUES (
                @trade_id, @stock_id, @opened_at, @closed_at, @quantity, @currency,
                @entry_price, @entry_fx_rate, @entry_price_eur, @entry_fee_eur,
                @exit_price, @exit_fx_rate, @exit_price_eur, @exit_fee_eur, @dividend_eur, @strategy, @notes
            )
        `);
        return this.db.transaction(() => {
            if (this.db.prepare('SELECT 1 FROM trades WHERE trade_id = ? UNION ALL SELECT 1 FROM trade_merge_events WHERE source_trade_id = ? LIMIT 1').get(trade.trade_id, trade.trade_id)) {
                const error = new Error('Trade ID already exists');
                error.code = 'SQLITE_CONSTRAINT_UNIQUE';
                throw error;
            }
            if (trade.closed_at) return { ...insert.run(trade), merged: false, mergedCount: 0 };
            const openTrades = this.db.prepare('SELECT * FROM trades WHERE stock_id = ? AND closed_at IS NULL ORDER BY opened_at, id').all(trade.stock_id);
            if (!openTrades.length) return { ...insert.run(trade), merged: false, mergedCount: 0 };
            return this.mergeOpenTradeRows(openTrades, trade);
        })();
    }

    mergeOpenTradeRows(openTrades, incoming = null) {
        if (!openTrades.length) throw new Error('No open trades to merge');
        const target = openTrades[0];
        const sources = [...openTrades.slice(1), ...(incoming ? [incoming] : [])];
        if (!sources.length) return { lastInsertRowid: target.id, merged: false, mergedCount: 0 };
        const rows = [target, ...sources];
        for (const row of rows) {
            if (!(Number.isFinite(Number(row.quantity)) && Number(row.quantity) > 0 && Number.isFinite(Number(row.entry_price)) && Number(row.entry_price) > 0 && Number.isFinite(Number(row.entry_price_eur)) && Number(row.entry_price_eur) > 0 && Number.isFinite(Number(row.entry_fee_eur)) && Number(row.entry_fee_eur) >= 0)) throw new Error('Cannot merge an invalid open trade');
        }
        const totalQuantity = rows.reduce((sum, row) => sum + Number(row.quantity), 0);
        const entryPriceEur = Number((rows.reduce((sum, row) => sum + Number(row.quantity) * Number(row.entry_price_eur), 0) / totalQuantity).toFixed(4));
        const currencies = [...new Set(rows.map(row => String(row.currency).toUpperCase()))];
        const currency = currencies.length === 1 ? currencies[0] : 'EUR';
        const entryPrice = currency === 'EUR'
            ? entryPriceEur
            : Number((rows.reduce((sum, row) => sum + Number(row.quantity) * Number(row.entry_price), 0) / totalQuantity).toFixed(4));
        const entryFxRate = currency === 'EUR' ? 1 : entryPriceEur / entryPrice;
        const strategies = [...new Set(rows.map(row => String(row.strategy || '').trim()).filter(Boolean))];
        const notes = [...new Set(rows.map(row => String(row.notes || '').trim()).filter(Boolean))];
        const mergeNote = `Merged ${sources.length} additional purchase${sources.length === 1 ? '' : 's'}; source details retained in trade_merge_events.`;
        if (!notes.includes(mergeNote)) notes.push(mergeNote);
        const audit = this.db.prepare('INSERT INTO trade_merge_events (target_trade_id, source_trade_id, source_trade_json) VALUES (?, ?, ?)');
        for (const source of sources) audit.run(target.id, source.trade_id, JSON.stringify(source));
        const existingSourceIds = sources.filter(source => Number.isInteger(Number(source.id))).map(source => Number(source.id));
        if (existingSourceIds.length) this.db.prepare(`DELETE FROM trades WHERE id IN (${existingSourceIds.map(() => '?').join(',')})`).run(...existingSourceIds);
        this.db.prepare(`UPDATE trades SET opened_at=?, quantity=?, currency=?, entry_price=?, entry_fx_rate=?, entry_price_eur=?, entry_fee_eur=?, strategy=?, notes=?, updated_at=datetime('now') WHERE id=?`).run(
            rows.map(row => row.opened_at).sort()[0], totalQuantity, currency, entryPrice, entryFxRate, entryPriceEur,
            rows.reduce((sum, row) => sum + Number(row.entry_fee_eur || 0), 0), strategies.join(' | '), notes.join(' | '), target.id
        );
        return { lastInsertRowid: target.id, merged: true, mergedCount: sources.length };
    }

    consolidateOpenTrades(stockId) {
        return this.db.transaction(() => {
            const openTrades = this.db.prepare('SELECT * FROM trades WHERE stock_id = ? AND closed_at IS NULL ORDER BY opened_at, id').all(stockId);
            return this.mergeOpenTradeRows(openTrades);
        })();
    }

    getTradeMergeEvents(targetTradeId) {
        return this.db.prepare('SELECT * FROM trade_merge_events WHERE target_trade_id = ? ORDER BY id').all(targetTradeId);
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

    closeTradeQuantity(id, trade, closeQuantity) {
        const quantity = Number(closeQuantity);
        if (!Number.isFinite(quantity) || quantity <= 0) throw new Error('Sale quantity must be greater than zero');
        return this.db.transaction(() => {
            const existing = this.db.prepare('SELECT * FROM trades WHERE id = ?').get(id);
            if (!existing) throw new Error('Trade not found');
            if (existing.closed_at) throw new Error('Trade is already closed; use explicit edit to correct it');
            const openQuantity = Number(existing.quantity);
            if (quantity > openQuantity) throw new Error(`Sale quantity cannot exceed the open quantity (${openQuantity})`);
            if (quantity === openQuantity) {
                this.updateTrade(id, { ...trade, quantity: openQuantity });
                return { id, partial: false };
            }

            const closedEntryFee = Number(existing.entry_fee_eur || 0) * quantity / openQuantity;
            const remainingQuantity = openQuantity - quantity;
            const remainingEntryFee = Number(existing.entry_fee_eur || 0) - closedEntryFee;
            const suffix = `-SELL-${Date.now()}-${crypto.randomBytes(3).toString('hex')}`;
            const closedTradeId = `${existing.trade_id}${suffix}`;
            const sourceNote = `Partial sale of ${quantity} from open trade ${existing.trade_id}.`;
            const notes = [String(trade.notes || '').trim(), sourceNote].filter(Boolean).join(' | ');
            this.db.prepare(`
                INSERT INTO trades (
                    trade_id, stock_id, opened_at, closed_at, quantity, currency,
                    entry_price, entry_fx_rate, entry_price_eur, entry_fee_eur,
                    exit_price, exit_fx_rate, exit_price_eur, exit_fee_eur, dividend_eur, strategy, notes
                ) VALUES (
                    @trade_id, @stock_id, @opened_at, @closed_at, @quantity, @currency,
                    @entry_price, @entry_fx_rate, @entry_price_eur, @entry_fee_eur,
                    @exit_price, @exit_fx_rate, @exit_price_eur, @exit_fee_eur, @dividend_eur, @strategy, @notes
                )
            `).run({ ...trade, trade_id: closedTradeId, quantity, entry_fee_eur: closedEntryFee, notes });
            this.db.prepare(`UPDATE trades SET quantity=?, entry_fee_eur=?, updated_at=datetime('now') WHERE id=?`)
                .run(remainingQuantity, remainingEntryFee, id);
            return { id, partial: true, closedTradeId };
        })();
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

    getBrokerMappings() { if (!this.db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name='broker_instrument_mappings'").get()) return []; return this.db.prepare(`SELECT m.*, s.name FROM broker_instrument_mappings m JOIN stocks s ON s.id=m.stock_id ORDER BY m.ticker`).all(); }
    getBrokerMapping(stockId) { if (!this.db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name='broker_instrument_mappings'").get()) return null; return this.db.prepare('SELECT * FROM broker_instrument_mappings WHERE stock_id=?').get(stockId); }
    saveBrokerMapping(input) { return this.db.prepare(`INSERT INTO broker_instrument_mappings (stock_id,broker,ticker,isin,exchange,currency,broker_metadata_json,source,verified_at) VALUES (@stock_id,@broker,@ticker,@isin,@exchange,@currency,@broker_metadata_json,@source,@verified_at) ON CONFLICT(stock_id) DO UPDATE SET broker=excluded.broker,ticker=excluded.ticker,isin=excluded.isin,exchange=excluded.exchange,currency=excluded.currency,broker_metadata_json=excluded.broker_metadata_json,source=excluded.source,verified_at=excluded.verified_at,updated_at=datetime('now')`).run(input); }
    addBrokerQuote(input) { return this.db.prepare(`INSERT INTO broker_quotes (broker,isin,exchange,bid_eur,ask_eur,last_eur,currency,quote_time,observed_at,source) VALUES (@broker,@isin,@exchange,@bid_eur,@ask_eur,@last_eur,@currency,@quote_time,@observed_at,@source)`).run(input); }
    getLatestBrokerQuote(isin, exchange) { if (!this.db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name='broker_quotes'").get()) return null; return this.db.prepare('SELECT * FROM broker_quotes WHERE broker=? AND isin=? AND exchange=? ORDER BY observed_at DESC LIMIT 1').get('trade_republic', isin, exchange); }
    getLatestBrokerAccount(accountRefMasked, maxAgeMs = 300000) {
        const row = this.db.prepare('SELECT * FROM broker_accounts WHERE broker=? AND account_ref_masked=? ORDER BY observed_at DESC LIMIT 1').get('trade_republic', accountRefMasked);
        return row && Date.now() - Date.parse(row.observed_at) >= 0 && Date.now() - Date.parse(row.observed_at) <= maxAgeMs ? row : null;
    }
    getLatestBrokerPosition(accountRefMasked, isin, maxAgeMs = 300000) {
        const row = this.db.prepare('SELECT * FROM broker_portfolio WHERE broker=? AND account_ref_masked=? AND isin=? ORDER BY observed_at DESC LIMIT 1').get('trade_republic', accountRefMasked, isin);
        return row && Date.now() - Date.parse(row.observed_at) >= 0 && Date.now() - Date.parse(row.observed_at) <= maxAgeMs ? row : null;
    }
    syncBrokerRows(table, rows) {
        const definitions = {
            broker_accounts: { columns:['broker','account_ref_masked','currency','cash_eur','available_cash_eur','observed_at','raw_hash'], key:['broker','account_ref_masked'] },
            broker_portfolio: { columns:['broker','account_ref_masked','isin','quantity','currency','value_eur','observed_at','external_ref_masked'], key:['broker','account_ref_masked','isin','observed_at'] },
            broker_orders: { columns:['broker','external_ref_masked','status','isin','side','quantity','amount_eur','occurred_at','observed_at','raw_hash'], key:['broker','external_ref_masked'] },
            broker_transactions: { columns:['broker','external_ref_masked','status','isin','quantity','amount_eur','occurred_at','observed_at','raw_hash'], key:['broker','external_ref_masked'] }
        };
        const definition = definitions[table];
        if (!definition) throw new Error('Invalid broker table');
        if (!rows.length) return 0;
        const updates = definition.columns.filter(column => !definition.key.includes(column)).map(column => `${column}=excluded.${column}`).join(',');
        const stmt = this.db.prepare(`INSERT INTO ${table} (${definition.columns.join(',')}) VALUES (${definition.columns.map(key=>`@${key}`).join(',')}) ON CONFLICT(${definition.key.join(',')}) DO UPDATE SET ${updates}`);
        return this.db.transaction(() => rows.reduce((count, row) => count + stmt.run(row).changes, 0))();
    }
    addBrokerReconciliationCandidate(input) { return this.db.prepare(`INSERT OR IGNORE INTO broker_reconciliation_candidates (broker,candidate_key,external_ref_masked,status,title,subtitle,amount_eur,currency,occurred_at,reason) VALUES (@broker,@candidate_key,@external_ref_masked,@status,@title,@subtitle,@amount_eur,@currency,@occurred_at,@reason)`).run(input); }
    getBrokerReconciliationCandidates() { return this.db.prepare('SELECT id,broker,candidate_key,external_ref_masked,status,title,subtitle,amount_eur,currency,occurred_at,reason,created_at FROM broker_reconciliation_candidates WHERE resolved_at IS NULL ORDER BY occurred_at DESC, id DESC').all(); }
    createBrokerOrderIntent(input) { return this.db.prepare(`INSERT INTO broker_order_intents (broker,idempotency_key,status,request_json,quote_id) VALUES (@broker,@idempotency_key,@status,@request_json,@quote_id)`).run(input); }
    getBrokerOrderIntent(key) { return this.db.prepare('SELECT * FROM broker_order_intents WHERE idempotency_key=?').get(key); }
    updateBrokerOrderIntent(id, input) { return this.db.prepare(`UPDATE broker_order_intents SET status=@status, response_json=@response_json, external_ref_masked=@external_ref_masked, reconciled_at=@reconciled_at, frozen_reason=@frozen_reason, updated_at=datetime('now') WHERE id=@id`).run({ ...input, id }); }
    getBrokerExecution(identity, broker = 'trade_republic') {
        const hashedIdentity = executionKey(broker, identity);
        return this.db.prepare('SELECT * FROM broker_executions WHERE execution_identity IN (?, ?) ORDER BY CASE WHEN execution_identity=? THEN 0 ELSE 1 END LIMIT 1').get(hashedIdentity, identity, hashedIdentity);
    }
    recordBrokerExecution(input) {
        const identity = executionKey(input.broker, input.execution_identity);
        const existing = this.db.prepare('SELECT * FROM broker_executions WHERE execution_identity IN (?, ?) ORDER BY CASE WHEN execution_identity=? THEN 0 ELSE 1 END LIMIT 1').get(identity, input.execution_identity, identity);
        if (existing) return { existing: true, execution: existing };
        const execution = this.db.transaction(() => {
            this.db.prepare(`INSERT INTO broker_executions (broker,execution_identity,order_intent_id,stock_id,account_ref_masked,side,quantity,execution_price_eur,fees_eur,executed_at,currency) VALUES (@broker,@execution_identity,@order_intent_id,@stock_id,@account_ref_masked,@side,@quantity,@execution_price_eur,@fees_eur,@executed_at,@currency)`).run({ ...input, execution_identity: identity });
            const executionLabel = identity.slice(0, 20);
            const note = 'Trade Republic confirmed execution';
            if (input.side === 'buy') {
                this.addTrade({ trade_id: `TR-EXEC-${executionLabel}`, stock_id: input.stock_id, opened_at: input.executed_at.replace('T', ' ').replace(/\.\d{3}Z$/, ''), closed_at: null, quantity: input.quantity, currency: 'EUR', entry_price: input.execution_price_eur, entry_fx_rate: 1, entry_price_eur: input.execution_price_eur, entry_fee_eur: input.fees_eur, exit_price: null, exit_fx_rate: null, exit_price_eur: null, exit_fee_eur: 0, dividend_eur: 0, strategy: 'trade_republic_execution', notes: note });
            } else {
                let remaining = Number(input.quantity);
                let remainingFee = Number(input.fees_eur);
                const lots = this.db.prepare('SELECT * FROM trades WHERE stock_id=? AND closed_at IS NULL ORDER BY opened_at, id').all(input.stock_id);
                for (const lot of lots) {
                    if (remaining <= 0) break;
                    const lotQuantity = Number(lot.quantity);
                    const sold = Math.min(remaining, lotQuantity);
                    const feeShare = sold === remaining ? remainingFee : Number(input.fees_eur) * sold / Number(input.quantity);
                    const when = input.executed_at.replace('T', ' ').replace(/\.\d{3}Z$/, '');
                    if (sold === lotQuantity) this.db.prepare("UPDATE trades SET closed_at=?, exit_price=?, exit_fx_rate=1, exit_price_eur=?, exit_fee_eur=?, notes=CASE WHEN trim(COALESCE(notes,''))='' THEN ? ELSE trim(notes) || ' | ' || ? END, updated_at=datetime('now') WHERE id=?").run(when, input.execution_price_eur, input.execution_price_eur, feeShare, note, note, lot.id);
                    else {
                        const closedEntryFee = Number(lot.entry_fee_eur) * sold / lotQuantity;
                        this.db.prepare("UPDATE trades SET quantity=quantity-?, entry_fee_eur=entry_fee_eur-?, updated_at=datetime('now') WHERE id=?").run(sold, closedEntryFee, lot.id);
                        this.db.prepare(`INSERT INTO trades (trade_id,stock_id,opened_at,closed_at,quantity,currency,entry_price,entry_fx_rate,entry_price_eur,entry_fee_eur,exit_price,exit_fx_rate,exit_price_eur,exit_fee_eur,dividend_eur,strategy,notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`).run(`TR-SELL-${executionLabel}-${lot.id}`, lot.stock_id, lot.opened_at, when, sold, lot.currency, lot.entry_price, lot.entry_fx_rate, lot.entry_price_eur, closedEntryFee, input.execution_price_eur, 1, input.execution_price_eur, feeShare, 0, 'trade_republic_execution', note);
                    }
                    remaining -= sold;
                    remainingFee -= feeShare;
                }
                if (remaining > 0) throw new Error('Execution would oversell Journal lots');
            }
            return this.db.prepare('SELECT * FROM broker_executions WHERE execution_identity=?').get(identity);
        })();
        return { existing: false, execution };
    }
}

module.exports = OpenStockDB;
