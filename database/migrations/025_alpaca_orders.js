'use strict';

module.exports = {
  version: 25,
  name: 'alpaca_orders',
  up(db) {
    db.exec(`
      CREATE TABLE IF NOT EXISTS alpaca_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        alpaca_alert_level_id INTEGER NOT NULL,
        client_order_id TEXT NOT NULL UNIQUE,
        broker_order_id TEXT UNIQUE,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
        qty INTEGER NOT NULL CHECK (qty > 0),
        limit_price_usd REAL NOT NULL CHECK (limit_price_usd > 0),
        notional_usd REAL NOT NULL CHECK (notional_usd > 0),
        time_in_force TEXT NOT NULL DEFAULT 'day',
        status TEXT NOT NULL DEFAULT 'SUBMITTING' CHECK (status IN (
          'SUBMITTING', 'NEW', 'PENDING_CANCEL', 'PARTIALLY_FILLED',
          'FILLED', 'CANCELED', 'EXPIRED', 'REJECTED', 'FAILED')),
        filled_qty REAL NOT NULL DEFAULT 0,
        filled_avg_price_usd REAL,
        last_error TEXT,
        submitted_at TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY (alpaca_alert_level_id) REFERENCES alpaca_alert_levels(id) ON DELETE CASCADE
      );
      -- At most one live order per alert level; also blocks double submits.
      CREATE UNIQUE INDEX IF NOT EXISTS idx_alpaca_orders_one_open_per_level
        ON alpaca_orders(alpaca_alert_level_id)
        WHERE status IN ('SUBMITTING', 'NEW', 'PENDING_CANCEL', 'PARTIALLY_FILLED');
      CREATE INDEX IF NOT EXISTS idx_alpaca_orders_created ON alpaca_orders(created_at, id);
    `);
  }
};
