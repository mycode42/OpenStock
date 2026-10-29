const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { EventEmitter } = require('node:events');
const {
    LabError, ResearchLab, buildJobConfig, diffParameters, parseFreeMemoryMb, parseLabEvents, summarizeProgress, validateJobRequest,
} = require('../server/algo-trader/research-lab');

const REPO = path.join(__dirname, '..', 'algo-trader');
const TODAY = '2026-10-29';

function fixtureRoot(t, { memoryKb = 9 * 1024 * 1024 } = {}) {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'lab-'));
    t.after(() => fs.rmSync(root, { recursive: true, force: true }));
    fs.mkdirSync(path.join(root, 'options', 'research'), { recursive: true });
    fs.copyFileSync(path.join(REPO, 'options', 'config.default.json'), path.join(root, 'options', 'config.default.json'));
    fs.copyFileSync(path.join(REPO, 'options', 'research', 'config.default.json'), path.join(root, 'options', 'research', 'config.default.json'));
    fs.copyFileSync(path.join(REPO, 'options', 'research', 'config.portfolio.json'), path.join(root, 'options', 'research', 'config.portfolio.json'));
    fs.mkdirSync(path.join(root, 'portfolio'), { recursive: true });
    fs.copyFileSync(path.join(REPO, 'portfolio', 'algo-strategy-config.default.json'), path.join(root, 'portfolio', 'algo-strategy-config.default.json'));
    fs.copyFileSync(path.join(REPO, 'portfolio', 'algo-strategy-rules.json'), path.join(root, 'portfolio', 'algo-strategy-rules.json'));
    fs.mkdirSync(path.join(root, 'data', 'options'), { recursive: true });
    const meminfo = path.join(root, 'meminfo');
    fs.writeFileSync(meminfo, `MemTotal: 16000000 kB\nMemAvailable: ${memoryKb} kB\n`);
    return { root, meminfo };
}

function stubSpawn() {
    const calls = [];
    const spawnImpl = (executable, args, options) => {
        calls.push({ executable, args, options });
        const child = new EventEmitter();
        child.pid = 4242;
        child.kill = () => true;
        return child;
    };
    return { spawnImpl, calls };
}

test('validateJobRequest accepts a valid options request and normalizes tickers', () => {
    const request = validateJobRequest({
        account: 'options', strategy: 'vrp', underlyings: ['aapl', 'MSFT', 'aapl'],
        start: '2023-01-01', end: '2026-06-30', profile: 'wider-wings',
    }, { today: TODAY });
    assert.deepEqual(request.underlyings, ['AAPL', 'MSFT']);
    assert.equal(request.start, '2023-01-01');
    assert.equal(request.profile, 'wider-wings');
});

test('validateJobRequest refuses equity, unknown strategies, oversize lists, bad dates and bad tickers', () => {
    const base = { account: 'options', strategy: 'vrp', underlyings: ['AAPL'] };
    assert.throws(() => validateJobRequest({ ...base, account: 'equity' }, { today: TODAY }), LabError);
    assert.throws(() => validateJobRequest({ ...base, strategy: 'llm_driven' }, { today: TODAY }), /strategy must be/);
    const many = Array.from({ length: 21 }, (_, i) => `T${i}`);
    assert.throws(() => validateJobRequest({ ...base, underlyings: many }, { today: TODAY }), /at most 20/);
    assert.throws(() => validateJobRequest({ ...base, start: '2019-12-31' }, { today: TODAY }), /start must be/);
    assert.throws(() => validateJobRequest({ ...base, end: '2026-11-01' }, { today: TODAY }), /no later than today/);
    assert.throws(() => validateJobRequest({ ...base, start: '2024-05-01', end: '2024-05-01' }, { today: TODAY }), /before end/);
    assert.throws(() => validateJobRequest({ ...base, start: '2024-02-30' }, { today: TODAY }), /start must be/);
    assert.throws(() => validateJobRequest({ ...base, underlyings: ['a b'] }, { today: TODAY }), /ticker/);
});

test('buildJobConfig disables champion writes and applies the profile as the strategy baseline', () => {
    const base = { champion: { enabled: true }, strategy_defaults: { vrp: { keep: 1 } } };
    const config = buildJobConfig(base, { strategy: 'vrp', parameters: { min_iv_minus_rv: 0.05 }, start: '2023-01-01', end: null });
    assert.equal(config.champion.enabled, false);
    assert.deepEqual(config.strategy_defaults.vrp, { keep: 1, min_iv_minus_rv: 0.05 });
    assert.deepEqual(config.date_range, { start: '2023-01-01', end: null });
    assert.equal(base.champion.enabled, true, 'base config must not be mutated');
});

test('parseLabEvents reads only prefixed lines and tolerates a partial last line', () => {
    const log = [
        'Options research vrp/AAPL: loading complete',
        'LAB_EVENT {"event": "started", "total": 2}',
        'LAB_EVENT {"event": "underlying_done", "underlying": "AAPL", "approved": true}',
        'LAB_EVENT {"event": "underlying_started", "underlying": "MSFT"',
    ].join('\n');
    const events = parseLabEvents(log);
    assert.deepEqual(events.map(item => item.event), ['started', 'underlying_done']);
    const progress = summarizeProgress(events);
    assert.equal(progress.total, 2);
    assert.equal(progress.done, 1);
    assert.deepEqual(progress.approved, ['AAPL']);
    assert.equal(progress.finished, false);
});

test('summarizeProgress shows the current underlying and failures', () => {
    const events = parseLabEvents([
        'LAB_EVENT {"event": "started", "total": 3}',
        'LAB_EVENT {"event": "underlying_error", "underlying": "AAPL", "error": "x"}',
        'LAB_EVENT {"event": "underlying_started", "underlying": "MSFT"}',
    ].join('\n'));
    const progress = summarizeProgress(events);
    assert.equal(progress.failures, 1);
    assert.equal(progress.current, 'MSFT');
});

test('diffParameters marks changed and new keys', () => {
    const diff = diffParameters({ min_iv_minus_rv: 0.04, wing_delta_max: 0.07 }, { min_iv_minus_rv: 0.03, wing_delta_max: 0.07, short_delta_min: 0.1 });
    const byKey = Object.fromEntries(diff.map(item => [item.key, item]));
    assert.equal(byKey.min_iv_minus_rv.changed, true);
    assert.equal(byKey.wing_delta_max.changed, false);
    assert.equal(byKey.short_delta_min.current, null);
});

test('parseFreeMemoryMb reads MemAvailable in megabytes', () => {
    assert.equal(parseFreeMemoryMb('MemTotal: 16000000 kB\nMemAvailable: 8388608 kB\n'), 8192);
    assert.throws(() => parseFreeMemoryMb('MemTotal: 1 kB\n'), /unavailable/);
});

test('startJob refuses a second job while one is running and writes a job record', async t => {
    const { root, meminfo } = fixtureRoot(t);
    const { spawnImpl, calls } = stubSpawn();
    const lab = new ResearchLab({ root, python: '/venv/bin/python', now: () => new Date('2026-10-29T18:00:00Z'), spawnImpl, meminfoPath: meminfo });
    const job = await lab.startJob({ account: 'options', strategy: 'vrp', underlyings: ['AAPL', 'MSFT'], start: '2023-01-01', end: '2026-06-30' });
    assert.equal(job.state, 'running');
    assert.equal(calls.length, 1);
    assert.deepEqual(calls[0].args.slice(0, 2), ['-m', 'options.research.lab_run']);
    assert.equal(calls[0].options.detached, true);
    const stored = JSON.parse(fs.readFileSync(path.join(lab.jobDir(job.id), 'config.json'), 'utf8'));
    assert.equal(stored.champion.enabled, false);
    assert.deepEqual(stored.date_range, { start: '2023-01-01', end: '2026-06-30' });
    await assert.rejects(lab.startJob({ account: 'options', strategy: 'vrp', underlyings: ['AAPL'] }), /already running/);
});

test('startJob refuses to run when free memory is below the floor', async t => {
    const { root, meminfo } = fixtureRoot(t, { memoryKb: 2 * 1024 * 1024 });
    const { spawnImpl, calls } = stubSpawn();
    const lab = new ResearchLab({ root, python: '/venv/bin/python', spawnImpl, meminfoPath: meminfo });
    await assert.rejects(lab.startJob({ account: 'options', strategy: 'vrp', underlyings: ['AAPL'] }), /memory is free/);
    assert.equal(calls.length, 0);
});

test('profiles accept only tunable grid keys and refuse duplicates', async t => {
    const { root, meminfo } = fixtureRoot(t);
    const lab = new ResearchLab({ root, meminfoPath: meminfo });
    await lab.saveProfile({ strategy: 'vrp', name: 'tight', parameters: { min_iv_minus_rv: 0.03 } });
    await assert.rejects(lab.saveProfile({ strategy: 'vrp', name: 'tight', parameters: { min_iv_minus_rv: 0.05 } }), /already exists/);
    await assert.rejects(lab.saveProfile({ strategy: 'vrp', name: 'bad', parameters: { enabled: false } }), /not a tunable/);
    await assert.rejects(lab.saveProfile({ strategy: 'vrp', name: 'bad name', parameters: { min_iv_minus_rv: 0.03 } }), /letters/);
});

test('promote writes only the changed keys and rollback restores the previous state', async t => {
    const { root, meminfo } = fixtureRoot(t);
    const lab = new ResearchLab({ root, meminfoPath: meminfo });
    await lab.saveProfile({ strategy: 'vrp', name: 'tight', parameters: { min_iv_minus_rv: 0.03 } });
    const preview = await lab.promotePreview('vrp', 'tight');
    assert.equal(preview.changes, 1);

    await lab.promote('vrp', 'tight');
    const override = JSON.parse(fs.readFileSync(path.join(root, 'data', 'options', 'config.json'), 'utf8'));
    assert.deepEqual(Object.keys(override), ['vrp']);
    assert.deepEqual(override.vrp, { min_iv_minus_rv: 0.03 });

    await lab.rollback('vrp');
    const restored = JSON.parse(fs.readFileSync(path.join(root, 'data', 'options', 'config.json'), 'utf8'));
    assert.deepEqual(restored.vrp, {});
    await assert.rejects(lab.rollback('vrp'), /nothing to roll back/);
});

test('promote keeps existing override values and rollback puts them back', async t => {
    const { root, meminfo } = fixtureRoot(t);
    fs.writeFileSync(path.join(root, 'data', 'options', 'config.json'), JSON.stringify({ vrp: { min_iv_minus_rv: 0.05 } }));
    const lab = new ResearchLab({ root, meminfoPath: meminfo });
    await lab.saveProfile({ strategy: 'vrp', name: 'tight', parameters: { min_iv_minus_rv: 0.03 } });
    await lab.promote('vrp', 'tight');
    await lab.rollback('vrp');
    const restored = JSON.parse(fs.readFileSync(path.join(root, 'data', 'options', 'config.json'), 'utf8'));
    assert.equal(restored.vrp.min_iv_minus_rv, 0.05);
});

test('overview returns one strategies list per account; equity is read-only for now', async t => {
    const { root, meminfo } = fixtureRoot(t);
    const lab = new ResearchLab({ root, meminfoPath: meminfo });
    const options = await lab.overview('options');
    const vrp = options.strategies.find(item => item.id === 'vrp');
    assert.ok(vrp.tunable.includes('min_iv_minus_rv'));
    assert.equal(options.start_available, true);
    assert.equal(options.limits.maxUnderlyings, 20);
    const equity = await lab.overview('equity');
    assert.equal(equity.start_available, false);
    assert.equal(equity.strategies.length, 7);
    assert.ok(equity.strategies.every(item => item.account === 'equity' && item.tunable.length === 0));
    await assert.rejects(lab.overview('crypto'), /account must be/);
});

test('research lab overview in demo mode returns the strategy list without Algo Trader files', () => {
    const { labOverview } = require('../server/demo-data');
    const overview = labOverview('options');
    assert.deepEqual(overview.strategies.map(strategy => strategy.id), ['vrp', 'long_volatility']);
    assert.equal(overview.start_available, false);
    assert.equal(overview.active_job, null);
    assert.throws(() => labOverview('bogus'), /account must be/);
});
