'use strict';

module.exports = {
  version: 24,
  name: 'alpaca_alert_outbox',
  up(db) {
    db.exec(`
      CREATE TABLE IF NOT EXISTS alpaca_alert_outbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        alpaca_alert_level_id INTEGER NOT NULL UNIQUE,
        delivery_key TEXT NOT NULL UNIQUE,
        symbol TEXT NOT NULL,
        message_html TEXT NOT NULL,
        price_usd REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'SENDING', 'FAILED', 'SENT')),
        attempt_count INTEGER NOT NULL DEFAULT 0,
        telegram_message_id TEXT,
        last_error TEXT,
        last_attempt_at TEXT,
        sent_at TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY (alpaca_alert_level_id) REFERENCES alpaca_alert_levels(id) ON DELETE CASCADE
      );
      CREATE INDEX IF NOT EXISTS idx_alpaca_alert_outbox_delivery ON alpaca_alert_outbox(status, created_at, id);
    `);
  }
};
