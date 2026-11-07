'use strict';

// Removes the legacy EUR data. Journal trades in EUR and their merge history are deleted. EUR stocks are
// deleted with their alert levels and price history, except the ones the Alpaca watchlist still points to
// (source_stock_id is NOT NULL, so those rows stay as the watchlist's provenance). USD rows are not touched.
function hasTable(db, name) {
  return Boolean(db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?").get(name));
}

module.exports = {
  version: 32,
  name: 'remove_legacy_eur_data',
  // initDatabase copies the database file before this runs.
  destructive: true,
  up(db) {
    if (!hasTable(db, 'trades') || !hasTable(db, 'stocks')) return;
    if (hasTable(db, 'trade_merge_events')) {
      db.exec("DELETE FROM trade_merge_events WHERE target_trade_id IN (SELECT id FROM trades WHERE currency = 'EUR')");
    }
    db.exec("DELETE FROM trades WHERE currency = 'EUR'");
    const watchlistSource = hasTable(db, 'alpaca_watchlist')
      ? 'SELECT source_stock_id FROM alpaca_watchlist'
      : 'SELECT NULL';
    db.exec(`DELETE FROM stocks WHERE currency = 'EUR' AND id NOT IN (${watchlistSource})`);
  },
};
