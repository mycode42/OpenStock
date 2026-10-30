const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const OpenStockDB = require('../database/db');
const { initDatabase } = require('../database/init');
const { buildJournalAnalytics } = require('../lib/alpaca-journal');
const { captureAlpacaEquitySnapshot, snapshotBucket } = require('../lib/alpaca-equity-snapshots');

function makeDb(t) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'openstock-equity-snap-'));
    const file = path.join(dir, 'test.db');
    initDatabase({ dbPath: file, seedDefaults: false, silent: true });
    const db = new OpenStockDB(file);
    t.after(() => { db.db.close(); fs.rmSync(dir, { recursive: true, force: true }); });
    return db;
}

const client = (equity, positions = []) => ({
    getAccount: async () => ({ equity: String(equity), cash: '400', last_equity: '1000' }),
    getPositions: async () => positions,
});

test('snapshots fall into five-minute buckets and repeat captures in one bucket update the same row', async t => {
    const db = makeDb(t);
    const at = new Date('2026-10-30T14:02:00Z');
    await captureAlpacaEquitySnapshot({ db, client: client(1000, [{ market_value: '600' }]), now: () => at });
    await captureAlpacaEquitySnapshot({ db, client: client(1010), now: () => new Date('2026-10-30T14:04:59Z') });
    await captureAlpacaEquitySnapshot({ db, client: client(1020), now: () => new Date('2026-10-30T14:05:00Z') });

    const rows = db.getAlpacaEquitySnapshots();
    assert.deepEqual(rows.map(row => row.snapshot_at), ['2026-10-30T14:00:00.000Z', '2026-10-30T14:05:00.000Z']);
    assert.equal(rows[0].equity_usd, 1010);
    assert.equal(rows[0].position_value_usd, 0);
    assert.equal(snapshotBucket(at), '2026-10-30T14:00:00.000Z');
});

test('an unavailable account is not stored as a zero-equity point', async t => {
    const db = makeDb(t);
    await assert.rejects(
        captureAlpacaEquitySnapshot({ db, client: client('n/a'), now: () => new Date('2026-10-30T14:00:00Z') }),
        /equity is unavailable/,
    );
    assert.equal(db.getAlpacaEquitySnapshots().length, 0);
});

test('the Journal curve uses broker daily history before the first snapshot and intraday points after it', () => {
    const history = {
        timestamp: [Date.parse('2026-10-28T20:00:00Z') / 1000, Date.parse('2026-10-29T20:00:00Z') / 1000],
        equity: [1000, 1005],
    };
    const snapshots = [
        { snapshot_at: '2026-10-30T14:00:00.000Z', equity_usd: 1010, cash_usd: 400, position_value_usd: 600 },
        { snapshot_at: '2026-10-30T14:05:00.000Z', equity_usd: 1012, cash_usd: 400, position_value_usd: 612 },
    ];
    const analytics = buildJournalAnalytics({ account: { equity: 1012, cash: 400 }, history, snapshots, now: new Date('2026-10-30T14:10:00Z') });

    assert.equal(analytics.equity_curve_resolution, 'intraday');
    assert.deepEqual(analytics.equity_curve.map(point => point.equity_usd), [1000, 1005, 1010, 1012]);
    assert.equal(analytics.equity_curve.at(-1).equity_usd, 1012);
});

test('without snapshots the Journal keeps the daily broker curve', () => {
    const history = { timestamp: [Date.parse('2026-10-29T20:00:00Z') / 1000], equity: [1005] };
    const analytics = buildJournalAnalytics({ account: { equity: 1005, cash: 0 }, history, snapshots: [], now: new Date('2026-10-30T14:10:00Z') });
    assert.equal(analytics.equity_curve_resolution, 'daily');
    assert.equal(analytics.equity_curve.length, 1);
});
