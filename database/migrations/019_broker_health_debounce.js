'use strict';

module.exports = {
  version: 19,
  name: 'broker_health_debounce',
  up(db) {
    const columns = db.prepare('PRAGMA table_info(broker_health_state)').all().map(row => row.name);
    if (!columns.includes('failure_streak')) db.exec('ALTER TABLE broker_health_state ADD COLUMN failure_streak INTEGER NOT NULL DEFAULT 0');
    if (!columns.includes('success_streak')) db.exec('ALTER TABLE broker_health_state ADD COLUMN success_streak INTEGER NOT NULL DEFAULT 0');
  }
};
