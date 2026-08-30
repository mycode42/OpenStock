-- OpenStock Database Schema
-- Single-user stock monitoring and trading journal

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

-- Monitored stocks
CREATE TABLE IF NOT EXISTS stocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL UNIQUE,
    name TEXT,
    currency TEXT DEFAULT 'USD',
    current_price_usd REAL,
    current_price_eur REAL,
    buy_threshold_eur REAL NOT NULL,
    sell_threshold_eur REAL NOT NULL,
    owned INTEGER DEFAULT 0 NOT NULL, -- 0 = Sold, 1 = Owned
    enabled INTEGER DEFAULT 1 NOT NULL, -- 0 = Disabled, 1 = Enabled
    last_price_update TEXT,
    last_exchange_rate REAL,
    trigger_state TEXT DEFAULT 'NONE', -- NONE, BUY_TRIGGERED, SELL_TRIGGERED
    created_at TEXT DEFAULT (datetime('now')) NOT NULL,
    updated_at TEXT DEFAULT (datetime('now')) NOT NULL
);

-- Trigger alert log (prevents duplicate alerts)
CREATE TABLE IF NOT EXISTS alerts_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_id INTEGER NOT NULL,
    alert_type TEXT NOT NULL, -- BUY, SELL
    price_eur REAL NOT NULL,
    threshold_eur REAL NOT NULL,
    sent_at TEXT DEFAULT (datetime('now')) NOT NULL,
    telegram_message_id INTEGER,
    FOREIGN KEY (stock_id) REFERENCES stocks(id) ON DELETE CASCADE
);

-- Exchange rate cache (for auditability)
CREATE TABLE IF NOT EXISTS exchange_rates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rate REAL NOT NULL,
    fetched_at TEXT DEFAULT (datetime('now')) NOT NULL
);

-- Trading journal
CREATE TABLE IF NOT EXISTS journal_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_id INTEGER NOT NULL,
    action TEXT NOT NULL, -- BUY, SELL
    price_eur REAL NOT NULL,
    quantity REAL NOT NULL,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now')) NOT NULL,
    updated_at TEXT DEFAULT (datetime('now')) NOT NULL,
    FOREIGN KEY (stock_id) REFERENCES stocks(id) ON DELETE CASCADE
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_stocks_symbol ON stocks(symbol);
CREATE INDEX IF NOT EXISTS idx_stocks_enabled ON stocks(enabled);
CREATE INDEX IF NOT EXISTS idx_alerts_log_stock_id ON alerts_log(stock_id);
CREATE INDEX IF NOT EXISTS idx_alerts_log_sent_at ON alerts_log(sent_at);
CREATE INDEX IF NOT EXISTS idx_journal_stock_id ON journal_entries(stock_id);
CREATE INDEX IF NOT EXISTS idx_journal_created_at ON journal_entries(created_at);

-- Trigger to update updated_at on stocks
CREATE TRIGGER IF NOT EXISTS update_stocks_updated_at
    AFTER UPDATE ON stocks
BEGIN
    UPDATE stocks SET updated_at = datetime('now') WHERE id = NEW.id;
END;

-- Trigger to update updated_at on journal_entries
CREATE TRIGGER IF NOT EXISTS update_journal_updated_at
    AFTER UPDATE ON journal_entries
BEGIN
    UPDATE journal_entries SET updated_at = datetime('now') WHERE id = NEW.id;
END;