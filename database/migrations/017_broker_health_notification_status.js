'use strict';

module.exports = {
  version: 17,
  name: 'broker_health_notification_status',
  up(db) {
    const columns = db.prepare('PRAGMA table_info(broker_health_state)').all().map(row => row.name);
    if (!columns.includes('last_notified_status')) {
      db.exec("ALTER TABLE broker_health_state ADD COLUMN last_notified_status TEXT CHECK (last_notified_status IN ('ONLINE', 'OFFLINE'))");
    }
  }
};
