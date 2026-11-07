'use strict';

// The one-time "delivery unknown" report is remembered in the outbox row, so a one-shot run does not repeat it.
module.exports = {
  version: 33,
  name: 'alpaca_outbox_unknown_reported',
  up(db) {
    db.exec('ALTER TABLE alpaca_alert_outbox ADD COLUMN unknown_reported_at TEXT');
  },
};
