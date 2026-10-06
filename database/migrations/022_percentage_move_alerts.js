module.exports = {
  version: 22,
  name: 'percentage_move_alerts',
  up(db) {
    if (!db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name='alert_levels'").get()) return;
    const columns = db.prepare('PRAGMA table_info(alert_levels)').all().map(column => column.name);
    if (!columns.includes('trigger_mode')) db.exec("ALTER TABLE alert_levels ADD COLUMN trigger_mode TEXT NOT NULL DEFAULT 'PRICE' CHECK (trigger_mode IN ('PRICE','PERCENT'))");
    if (!columns.includes('change_pct')) db.exec('ALTER TABLE alert_levels ADD COLUMN change_pct REAL');
    if (!columns.includes('reference_price_eur')) db.exec('ALTER TABLE alert_levels ADD COLUMN reference_price_eur REAL');
    db.exec(`CREATE INDEX IF NOT EXISTS idx_alert_levels_trigger_mode ON alert_levels(stock_id, trigger_mode, enabled, retired)`);
  }
};
