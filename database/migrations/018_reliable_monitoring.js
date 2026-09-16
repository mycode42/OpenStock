'use strict';

// New tables only: prior migrations may already have been applied to a live DB.
module.exports = {
  version: 18,
  name: 'reliable_monitoring_outbox_and_history_runs',
  up(db) {
    db.exec(`CREATE TABLE IF NOT EXISTS alert_outbox (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      alert_level_id INTEGER NOT NULL UNIQUE,
      delivery_key TEXT NOT NULL UNIQUE,
      stock_id INTEGER NOT NULL,
      message_html TEXT NOT NULL,
      price_eur REAL NOT NULL,
      status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','SENDING','SENT','FAILED')),
      attempt_count INTEGER NOT NULL DEFAULT 0,
      last_attempt_at TEXT,
      sent_at TEXT,
      telegram_message_id TEXT,
      last_error TEXT,
      created_at TEXT NOT NULL DEFAULT (datetime('now')),
      updated_at TEXT NOT NULL DEFAULT (datetime('now')),
      FOREIGN KEY(alert_level_id) REFERENCES alert_levels(id) ON DELETE CASCADE,
      FOREIGN KEY(stock_id) REFERENCES stocks(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_alert_outbox_pending ON alert_outbox(status, created_at);
    CREATE TABLE IF NOT EXISTS history_backfill_runs (
      stock_id INTEGER NOT NULL,
      source TEXT NOT NULL,
      range TEXT NOT NULL,
      status TEXT NOT NULL CHECK (status IN ('PENDING','RUNNING','SUCCEEDED','FAILED')),
      points_saved INTEGER NOT NULL DEFAULT 0,
      started_at TEXT,
      completed_at TEXT,
      last_error TEXT,
      PRIMARY KEY(stock_id, source, range),
      FOREIGN KEY(stock_id) REFERENCES stocks(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_history_backfill_runs_pending ON history_backfill_runs(source, status);
    `);
  }
};
