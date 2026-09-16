'use strict';

module.exports = {
  version: 16,
  name: 'broker_health_state',
  up(db) {
    db.exec(`
      CREATE TABLE IF NOT EXISTS broker_health_state (
        broker TEXT PRIMARY KEY,
        status TEXT NOT NULL CHECK (status IN ('ONLINE', 'OFFLINE')),
        checked_at TEXT NOT NULL,
        changed_at TEXT NOT NULL,
        last_notified_at TEXT,
        last_error TEXT
      );
    `);
  }
};
