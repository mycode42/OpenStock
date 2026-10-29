'use strict';

module.exports = {
  version: 28,
  name: 'alpaca_outbox_per_fire',
  up(db) {
    // One outbox row per firing, not per level, so a rearmed level can notify again.
    // Existing rows keep their delivery keys; new keys include the fire time.
    db.exec(`
      CREATE TABLE alpaca_alert_outbox_new (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        alpaca_alert_level_id INTEGER NOT NULL,
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
      INSERT INTO alpaca_alert_outbox_new SELECT * FROM alpaca_alert_outbox;
      DROP TABLE alpaca_alert_outbox;
      ALTER TABLE alpaca_alert_outbox_new RENAME TO alpaca_alert_outbox;
      CREATE INDEX IF NOT EXISTS idx_alpaca_alert_outbox_delivery ON alpaca_alert_outbox(status, created_at, id);
      CREATE INDEX IF NOT EXISTS idx_alpaca_alert_outbox_level ON alpaca_alert_outbox(alpaca_alert_level_id, id);
    `);
  }
};
