'use strict';

module.exports = {
  version: 30,
  name: 'alpaca_order_mode',
  up(db) {
    // Which account an order went to. Existing rows were all placed on paper.
    db.exec(`
      ALTER TABLE alpaca_orders ADD COLUMN mode TEXT NOT NULL DEFAULT 'paper' CHECK (mode IN ('paper', 'live'));
    `);
  }
};
