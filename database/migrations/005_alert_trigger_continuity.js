module.exports = {
  version: 5,
  name: 'alert_trigger_continuity',
  up(db) {
    // Reconcile normalized rows with the legacy debounce state before the
    // monitor can run again. Retired levels are always disabled and inactive.
    db.exec(`UPDATE alert_levels SET active = 0, enabled = 0 WHERE triggered = 1 OR retired = 1`);
    db.exec(`UPDATE alert_levels
      SET triggered = 1, retired = 1, active = 0, enabled = 0,
          triggered_at = COALESCE((SELECT MAX(sent_at) FROM alerts_log l WHERE l.alert_level_id = alert_levels.id), triggered_at, datetime('now')),
          retired_at = COALESCE((SELECT MAX(sent_at) FROM alerts_log l WHERE l.alert_level_id = alert_levels.id), retired_at, datetime('now'))
      WHERE (side = 'BUY' AND EXISTS (SELECT 1 FROM stocks s WHERE s.id = alert_levels.stock_id AND s.trigger_state = 'BUY_TRIGGERED'))
         OR (side = 'SELL' AND EXISTS (SELECT 1 FROM stocks s WHERE s.id = alert_levels.stock_id AND s.trigger_state = 'SELL_TRIGGERED'))`);
  }
};
