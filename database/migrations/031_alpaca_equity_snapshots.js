'use strict';

module.exports = {
  version: 31,
  name: 'alpaca_equity_snapshots',
  up(db) {
    // One row per five-minute bucket for the manual Alpaca account, so the Journal curve is continuous like the Algo and options accounts.
    db.exec(`
      CREATE TABLE IF NOT EXISTS alpaca_equity_snapshots (
        snapshot_at TEXT PRIMARY KEY,
        equity_usd REAL NOT NULL,
        cash_usd REAL NOT NULL,
        position_value_usd REAL NOT NULL
      );
    `);
  }
};
