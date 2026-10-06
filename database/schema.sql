-- OpenStock Database Schema
-- Single-user stock monitoring

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

-- Monitored stocks
CREATE TABLE IF NOT EXISTS stocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL UNIQUE,
    isin TEXT,
    name TEXT,
    currency TEXT DEFAULT 'USD',
    current_price_usd REAL,
    current_price_eur REAL,
    buy_threshold_eur REAL NOT NULL,
    sell_threshold_eur REAL NOT NULL,
    owned INTEGER DEFAULT 0 NOT NULL, -- 0 = Sold, 1 = Owned
    enabled INTEGER DEFAULT 1 NOT NULL, -- 0 = Disabled, 1 = Enabled
    archived INTEGER DEFAULT 0 NOT NULL, -- 0 = active catalogue, 1 = historical only
    asset_type TEXT NOT NULL DEFAULT 'stock', -- stock or crypto
    last_price_update TEXT,
    last_exchange_rate REAL,
    trigger_state TEXT DEFAULT 'NONE', -- NONE, BUY_TRIGGERED, SELL_TRIGGERED
    created_at TEXT DEFAULT (datetime('now')) NOT NULL,
    updated_at TEXT DEFAULT (datetime('now')) NOT NULL
);

-- Trigger alert log (prevents duplicate alerts)
CREATE TABLE IF NOT EXISTS alerts_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_id INTEGER,
    alert_type TEXT NOT NULL, -- BUY, SELL
    price_eur REAL NOT NULL,
    threshold_eur REAL NOT NULL,
    sent_at TEXT DEFAULT (datetime('now')) NOT NULL,
    telegram_message_id INTEGER,
    alert_level_id INTEGER,
    FOREIGN KEY (stock_id) REFERENCES stocks(id) ON DELETE SET NULL,
    FOREIGN KEY (alert_level_id) REFERENCES alert_levels(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS alert_levels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_id INTEGER NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    price_eur REAL NOT NULL CHECK (price_eur > 0),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    triggered INTEGER NOT NULL DEFAULT 0 CHECK (triggered IN (0, 1)),
    retired INTEGER NOT NULL DEFAULT 0 CHECK (retired IN (0, 1)),
    triggered_at TEXT,
    retired_at TEXT,
    trigger_mode TEXT NOT NULL DEFAULT 'PRICE' CHECK (trigger_mode IN ('PRICE','PERCENT')),
    change_pct REAL,
    reference_price_eur REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (stock_id) REFERENCES stocks(id) ON DELETE CASCADE,
    UNIQUE (stock_id, side, price_eur)
);
CREATE INDEX IF NOT EXISTS idx_alert_levels_stock_state ON alert_levels(stock_id, enabled, active, triggered, retired);
CREATE INDEX IF NOT EXISTS idx_alert_levels_side ON alert_levels(stock_id, side);

-- Exchange rate cache (for auditability)
CREATE TABLE IF NOT EXISTS exchange_rates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rate REAL NOT NULL,
    fetched_at TEXT DEFAULT (datetime('now')) NOT NULL
);

-- Immutable price observations collected by the monitor and manual refreshes.
-- `recorded_at` is when OpenStock observed the quote; `quote_time` is the
-- timestamp supplied by the source when it is available.
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_id INTEGER NOT NULL,
    price_usd REAL,
    price_eur REAL,
    exchange_rate REAL,
    source TEXT NOT NULL,
    quote_time TEXT,
    recorded_at TEXT DEFAULT (datetime('now')) NOT NULL,
    FOREIGN KEY (stock_id) REFERENCES stocks(id) ON DELETE CASCADE,
    UNIQUE (stock_id, recorded_at)
);

-- Capital movements are deliberately separate from trade performance. Amounts
-- are in EUR and always positive; the event type determines their direction.
CREATE TABLE IF NOT EXISTS capital_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL CHECK (event_type IN ('INITIAL', 'DEPOSIT', 'WITHDRAWAL', 'FINANCING')),
    amount_eur REAL NOT NULL CHECK (amount_eur > 0),
    occurred_at TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')) NOT NULL
);

-- One row represents one trade lot. A trade without closed_at is an open
-- position. Entry/exit conversion rates and EUR prices are frozen at entry so
-- later performance calculations are reproducible.
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT NOT NULL UNIQUE,
    stock_id INTEGER NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    quantity REAL NOT NULL CHECK (quantity > 0),
    currency TEXT NOT NULL DEFAULT 'EUR',
    entry_price REAL NOT NULL CHECK (entry_price > 0),
    entry_fx_rate REAL NOT NULL DEFAULT 1 CHECK (entry_fx_rate > 0),
    entry_price_eur REAL NOT NULL CHECK (entry_price_eur > 0),
    entry_fee_eur REAL NOT NULL DEFAULT 0 CHECK (entry_fee_eur >= 0),
    exit_price REAL,
    exit_fx_rate REAL,
    exit_price_eur REAL,
    exit_fee_eur REAL NOT NULL DEFAULT 0 CHECK (exit_fee_eur >= 0),
    dividend_eur REAL NOT NULL DEFAULT 0 CHECK (dividend_eur >= 0),
    strategy TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')) NOT NULL,
    updated_at TEXT DEFAULT (datetime('now')) NOT NULL,
    FOREIGN KEY (stock_id) REFERENCES stocks(id) ON DELETE RESTRICT,
    CHECK ((closed_at IS NULL AND exit_price IS NULL AND exit_fx_rate IS NULL AND exit_price_eur IS NULL)
        OR (closed_at IS NOT NULL AND exit_price > 0 AND exit_fx_rate > 0 AND exit_price_eur > 0))
);

-- Open purchases for the same stock are consolidated into one position row.
-- This audit table preserves every merged source trade exactly as entered.
CREATE TABLE IF NOT EXISTS trade_merge_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_trade_id INTEGER NOT NULL,
    source_trade_id TEXT NOT NULL UNIQUE,
    source_trade_json TEXT NOT NULL,
    merged_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (target_trade_id) REFERENCES trades(id) ON DELETE RESTRICT
);

-- Periodic snapshots let historical equity and drawdown remain auditable even
-- when today's positions/quotes change. Snapshots are taken by the monitor.
CREATE TABLE IF NOT EXISTS equity_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_at TEXT NOT NULL UNIQUE,
    cash_eur REAL NOT NULL,
    position_value_eur REAL NOT NULL,
    equity_eur REAL NOT NULL,
    net_contributions_eur REAL NOT NULL,
    financing_eur REAL NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'monitor'
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_stocks_symbol ON stocks(symbol);
CREATE INDEX IF NOT EXISTS idx_stocks_enabled ON stocks(enabled);
CREATE INDEX IF NOT EXISTS idx_alerts_log_stock_id ON alerts_log(stock_id);
CREATE INDEX IF NOT EXISTS idx_alerts_log_sent_at ON alerts_log(sent_at);
CREATE INDEX IF NOT EXISTS idx_price_history_stock_recorded_at ON price_history(stock_id, recorded_at);
CREATE INDEX IF NOT EXISTS idx_capital_events_occurred_at ON capital_events(occurred_at);
CREATE INDEX IF NOT EXISTS idx_trades_stock_opened_at ON trades(stock_id, opened_at);
CREATE INDEX IF NOT EXISTS idx_trades_closed_at ON trades(closed_at);
CREATE INDEX IF NOT EXISTS idx_trade_merge_events_target ON trade_merge_events(target_trade_id, merged_at);
CREATE INDEX IF NOT EXISTS idx_equity_snapshots_snapshot_at ON equity_snapshots(snapshot_at);

CREATE TRIGGER IF NOT EXISTS update_trades_updated_at
    AFTER UPDATE ON trades
BEGIN
    UPDATE trades SET updated_at = datetime('now') WHERE id = NEW.id;
END;
