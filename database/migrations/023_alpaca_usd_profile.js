'use strict';

module.exports = {
  version: 23,
  name: 'alpaca_usd_profile',
  up(db) {
    db.exec(`
      CREATE TABLE IF NOT EXISTS alpaca_watchlist (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL UNIQUE,
        name TEXT,
        source_stock_id INTEGER NOT NULL UNIQUE,
        enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
        archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
        copied_at TEXT NOT NULL DEFAULT (datetime('now')),
        copied_fx_rate REAL NOT NULL,
        FOREIGN KEY (source_stock_id) REFERENCES stocks(id) ON DELETE RESTRICT
      );

      CREATE TABLE IF NOT EXISTS alpaca_alert_levels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        watchlist_id INTEGER NOT NULL,
        source_alert_level_id INTEGER NOT NULL UNIQUE,
        side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
        price_usd REAL NOT NULL CHECK (price_usd > 0),
        enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
        active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
        triggered INTEGER NOT NULL DEFAULT 0 CHECK (triggered IN (0, 1)),
        retired INTEGER NOT NULL DEFAULT 0 CHECK (retired IN (0, 1)),
        triggered_at TEXT,
        retired_at TEXT,
        trigger_mode TEXT NOT NULL DEFAULT 'PRICE' CHECK (trigger_mode IN ('PRICE', 'PERCENT')),
        change_pct REAL,
        reference_price_usd REAL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY (watchlist_id) REFERENCES alpaca_watchlist(id) ON DELETE CASCADE
      );
      CREATE INDEX IF NOT EXISTS idx_alpaca_alert_levels_watchlist_state
        ON alpaca_alert_levels(watchlist_id, enabled, active, triggered, retired);

      CREATE TABLE IF NOT EXISTS alpaca_profile_migrations (
        name TEXT PRIMARY KEY,
        completed_at TEXT NOT NULL DEFAULT (datetime('now')),
        fx_rate REAL NOT NULL,
        symbol_count INTEGER NOT NULL,
        alert_count INTEGER NOT NULL
      );
    `);
  }
};
