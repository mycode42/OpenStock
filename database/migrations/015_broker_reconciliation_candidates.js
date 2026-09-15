module.exports = {
  version: 15,
  name: 'broker_reconciliation_candidates',
  up(db) {
    db.exec(`CREATE TABLE IF NOT EXISTS broker_reconciliation_candidates (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      broker TEXT NOT NULL,
      candidate_key TEXT NOT NULL UNIQUE,
      external_ref_masked TEXT,
      status TEXT NOT NULL,
      title TEXT,
      subtitle TEXT,
      amount_eur REAL,
      currency TEXT,
      occurred_at TEXT,
      reason TEXT NOT NULL,
      resolved_at TEXT,
      created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_broker_reconciliation_candidates_open
      ON broker_reconciliation_candidates(resolved_at, occurred_at);`);
  }
};
