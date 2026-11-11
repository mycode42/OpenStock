'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { readOptionsResearch, summarizeOptionsResearch } = require('../server/algo-trader/options');

function report(date, status = 'completed') {
    return { run_date_et: date, created_at: `${date}T22:00:00Z`, status, strategies: { vrp: { status: 'completed', approved: false } } };
}

async function archive(files) {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'options-research-'));
    const directory = path.join(root, 'options', 'research', 'simulation');
    await fs.mkdir(directory, { recursive: true });
    for (const [name, value] of Object.entries(files)) {
        await fs.writeFile(path.join(directory, name), JSON.stringify(value));
    }
    return { root, directory };
}

test('unchanged archive reports are served from the summary cache', async () => {
    const { root } = await archive({ '2026-11-08.json': report('2026-11-08'), 'latest.json': report('2026-11-09') });
    const first = await readOptionsResearch(root);
    const second = await readOptionsResearch(root);
    assert.equal(first.history.length, 2);
    assert.equal(second.latest, first.latest);
    for (const item of second.history) {
        assert.ok(first.history.includes(item), 'same summary object is reused for an unchanged file');
    }
});

test('a rewritten report is summarized again', async () => {
    const { root, directory } = await archive({ 'latest.json': report('2026-11-09', 'running') });
    const before = await readOptionsResearch(root);
    assert.equal(before.latest.status, 'running');
    await fs.writeFile(path.join(directory, 'latest.json'), JSON.stringify(report('2026-11-09', 'completed')));
    const later = new Date(Date.now() + 5000);
    await fs.utimes(path.join(directory, 'latest.json'), later, later);
    const after = await readOptionsResearch(root);
    assert.equal(after.latest.status, 'completed');
});

test('a malformed latest report falls back to the newest dated report', async () => {
    const { root, directory } = await archive({ '2026-11-08.json': report('2026-11-08') });
    await fs.writeFile(path.join(directory, 'latest.json'), '{"truncated": ');
    const result = await readOptionsResearch(root);
    assert.equal(result.latest.date, '2026-11-08');
});

test('a missing research folder reports no research', async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'options-empty-'));
    assert.deepEqual(await readOptionsResearch(root), { latest: null, history: [] });
});

test('summaries keep the strategy count and carry P/L from the simulation KPIs', () => {
    const summary = summarizeOptionsResearch({ ...report('2026-11-09'), simulation_kpis: { vrp: { net_pnl_usd: 120, trades: 3, worst_drawdown: 0.05 } } });
    assert.equal(summary.active_trials, 1);
    assert.equal(summary.net_pnl_usd, 120);
    assert.equal(summary.trades, 3);
    assert.equal(summary.worst_drawdown, 0.05);
});

test('archives without simulation KPIs still carry P/L from their exploratory replays', () => {
    const replay = (pnls, drawdown) => ({
        trades: pnls.map(pnl => ({ pnl })),
        metrics: { max_drawdown: drawdown },
    });
    const summary = summarizeOptionsResearch({
        ...report('2026-11-10'),
        strategies: {
            vrp: { status: 'completed', underlyings: { SPY: { exploratory_replay: replay([50, -20, 30], 0.04) } } },
            long_volatility: { status: 'completed', underlyings: { SPY: { exploratory_replay: replay([-100], 0.1) } } },
        },
    });
    assert.equal(summary.sleeves.vrp.trades, 3);
    assert.equal(summary.sleeves.vrp.net_pnl_usd, 60);
    assert.equal(summary.sleeves.vrp.profit_factor, 4);
    assert.equal(summary.sleeves.long_volatility.net_pnl_usd, -100);
    assert.equal(summary.net_pnl_usd, -40);
    assert.equal(summary.trades, 4);
    assert.equal(summary.worst_drawdown, 0.1);
});
