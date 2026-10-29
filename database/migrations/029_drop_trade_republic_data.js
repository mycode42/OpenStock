'use strict';

// Trade Republic was removed from the product. Its broker mirror tables, its
// history backfill ledger, and the price rows it produced are dropped. Journal
// trades and their merge history stay: they are the user's records.
const TR_TABLES = [
  'broker_accounts', 'broker_executions', 'broker_health_state', 'broker_instrument_mappings',
  'broker_order_intents', 'broker_orders', 'broker_portfolio', 'broker_profile_settings',
  'broker_quotes', 'broker_reconciliation_candidates', 'broker_sync_state', 'broker_transactions',
  'history_backfill_runs',
];

module.exports = {
  version: 29,
  name: 'drop_trade_republic_data',
  up(db) {
    for (const table of TR_TABLES) db.exec(`DROP TABLE IF EXISTS ${table}`);
    if (db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name='price_history'").get()) {
      db.exec(`DELETE FROM price_history WHERE source LIKE 'trade_republic%' OR source LIKE 'manual_refresh_trade_republic%' OR source = 'pytr_rates'`);
    }
  },
};
