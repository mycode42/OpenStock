'use strict';

module.exports = {
  version: 26,
  name: 'alpaca_auto_orders',
  up(db) {
    // Opt-in per level: when it fires, the monitor also places a manual-style limit order.
    db.exec(`
      ALTER TABLE alpaca_alert_levels ADD COLUMN auto_order INTEGER NOT NULL DEFAULT 0 CHECK (auto_order IN (0, 1));
    `);
  }
};
