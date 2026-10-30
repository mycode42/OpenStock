const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const Database = require('better-sqlite3');
const { readSnapshotHistory } = require('../server/algo-trader/equity');

// Same table the algo state database creates in reporting/snapshot_store.py.
const SCHEMA = `
CREATE TABLE IF NOT EXISTS account_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trading_mode TEXT NOT NULL,
    account_id TEXT NOT NULL,
    snapshot_kind TEXT NOT NULL CHECK (snapshot_kind IN ('intraday', 'close')),
    market_day TEXT NOT NULL,
    taken_at TEXT NOT NULL,
    equity REAL NOT NULL,
    payload_json TEXT NOT NULL,
    UNIQUE (trading_mode, account_id, snapshot_kind, taken_at)
);
`;

function snapshotDatabase(record, { accountId, tradingMode, kind = 'intraday' }) {
    return { record: { ...record, trading_mode: tradingMode, account_id: accountId, snapshot_kind: kind }, accountId, tradingMode, kind };
}

function writeRows(dataDir, rows) {
    const db = new Database(path.join(dataDir, 'algo_state.sqlite3'));
    db.exec(SCHEMA);
    const insert = db.prepare(`INSERT INTO account_snapshots
        (trading_mode, account_id, snapshot_kind, market_day, taken_at, equity, payload_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)`);
    for (const { record, accountId, tradingMode, kind } of rows) {
        insert.run(tradingMode, accountId, kind, record.date, record.timestamp, record.equity, JSON.stringify(record));
    }
    db.close();
}

test('intraday equity history keeps a full 24/7 day of 5-minute snapshots', async () => {
    const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-algo-equity-'));
    const start = Date.parse('2026-10-29T00:00:00Z');
    const rows = Array.from({ length: 288 }, (_, index) => snapshotDatabase({
        date: '2026-10-29', timestamp: new Date(start + index * 5 * 60000).toISOString(), equity: 100000 + index,
    }, { accountId: 'algo-paper', tradingMode: 'algo' }));
    writeRows(dataDir, rows);
    try {
        const history = await readSnapshotHistory(dataDir, 'algo-paper');
        assert.equal(history.length, 288);
        assert.equal(history[0].timestamp, new Date(start).toISOString());
        assert.equal(history.at(-1).equity, 100287);
    } finally {
        fs.rmSync(dataDir, { recursive: true, force: true });
    }
});

test('options snapshot history reads only options rows for the options account', async () => {
    const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-options-snapshots-'));
    writeRows(dataDir, [
        snapshotDatabase({ date: '2026-10-29', timestamp: '2026-10-29T14:00:00.000Z', equity: 100100 }, { accountId: 'options-paper-test', tradingMode: 'options' }),
        snapshotDatabase({ date: '2026-10-29', timestamp: '2026-10-29T14:00:00.000Z', equity: 99000 }, { accountId: 'equity-paper-test', tradingMode: 'algo' }),
    ]);
    try {
        const history = await readSnapshotHistory(dataDir, 'options-paper-test', { tradingMode: 'options' });
        assert.deepEqual(history.map(point => point.equity), [100100]);
    } finally {
        fs.rmSync(dataDir, { recursive: true, force: true });
    }
});
