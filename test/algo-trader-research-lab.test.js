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

test('a short equity range is accepted: the pipeline decides from stored bars whether it can be verified', () => {
    const base = { account: 'equity', strategies: ['swing_mean_reversion'], underlyings: ['SPCX'] };
    assert.doesNotThrow(() => validateJobRequest({ ...base, start: '2026-08-03', end: '2026-11-06' }, { today: TODAY }));
    assert.doesNotThrow(() => validateJobRequest({ ...base, start: '2025-01-02', end: '2026-11-06' }, { today: TODAY }));
});

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
    assert.throws(() => validateJobRequest({ account: 'equity', strategies: ['vrp'] }, { today: TODAY }), /unknown equity strategies: vrp/);
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

test('intraday profiles run in lab tests but plan apply refuses them until they go through promotion', async t => {
    const { root, meminfo } = fixtureRoot(t);
    const lab = new ResearchLab({ root, python: '/venv/bin/python', meminfoPath: meminfo });
    await lab.setPlan('equity', { intraday_momentum: 'conservative' });
    const preview = await lab.equityPlanPreview();
    assert.deepEqual(preview.not_applied, [{ strategy: 'intraday_momentum', profile: 'conservative' }]);
    assert.equal(preview.changes, 0);
    await assert.rejects(() => lab.equityPlanApply(), err => err.status === 409 && /not applied yet/.test(err.message));
});

test('an equity run reports every pipeline stage, and stages that never ran read not run once it finishes', async t => {
    const { root, meminfo } = fixtureRoot(t);
    const lab = new ResearchLab({ root, python: '/venv/bin/python', meminfoPath: meminfo });
    const id = '2026-01-01T00-00-00-000Z-stages';
    const dir = path.join(root, 'data', 'research-lab', 'jobs', id);
    fs.mkdirSync(path.join(dir, 'output'), { recursive: true });
    fs.writeFileSync(path.join(dir, 'job.json'), JSON.stringify({
        id, account: 'equity', strategy: 'intraday_momentum', strategies: ['intraday_momentum'],
        underlyings: ['AAPL'], state: 'finished', created_at: '2026-01-01T00:00:00.000Z',
    }));
    fs.writeFileSync(path.join(dir, 'output', 'latest-progress.json'), JSON.stringify({
        stages: { data_refresh: { status: 'completed' }, discovery: { status: 'completed' } },
        strategies: { discovery: { intraday_momentum: { status: 'completed' } } },
    }));
    const job = await lab.jobStatus(id);
    const byKey = Object.fromEntries(job.stages.rows.map(row => [row.key, row]));
    assert.equal(byKey.data_refresh.status, 'completed');
    assert.deepEqual(byKey.discovery.strategies, { done: 1, total: 1 });
    assert.equal(byKey.verification.status, 'not_run');
    assert.equal(job.stages.percent, 100);
});

test('the demo lab shows the sample profiles of every equity strategy and refuses changes', () => {
    const demo = require('../server/demo-data');
    const overview = demo.labOverview('equity');
    assert.equal(overview.read_only, true);
    assert.equal(overview.start_available, false);
    assert.equal(overview.strategies.length, 7);
    for (const strategy of overview.strategies) {
        assert.deepEqual(strategy.profiles.map(profile => profile.name), ['standard', 'conservative', 'high-risk'], strategy.id);
        assert.ok(strategy.tunable.length > 0, strategy.id);
    }
});

test('free memory falls back to the operating system figure where /proc/meminfo does not exist', async t => {
    const { root } = fixtureRoot(t);
    const lab = new ResearchLab({ root, python: '/venv/bin/python', meminfoPath: path.join(root, 'no-such-meminfo') });
    const free = await lab.freeMemoryMb();
    assert.ok(Number.isInteger(free) && free >= 0);
});

test('mean reversion filters are profile keys: a custom profile can set the slope mode and the efficiency-ratio filter', async t => {
    const { root, meminfo } = fixtureRoot(t);
    const lab = new ResearchLab({ root, python: '/venv/bin/python', meminfoPath: meminfo, checkProfiles: async () => {} });
    const saved = await lab.saveProfile({
        account: 'equity', strategy: 'swing_mean_reversion', name: 'vol-scaled',
        parameters: { slope_mode: 'volatility_scaled', slope_volatility_multiple: 1.5, regime_filter: 'er', efficiency_ratio_max: 0.4 },
    });
    assert.deepEqual(saved.at(-1).parameters, { slope_mode: 'volatility_scaled', slope_volatility_multiple: 1.5, regime_filter: 'er', efficiency_ratio_max: 0.4 });
    await assert.rejects(lab.saveProfile({ account: 'equity', strategy: 'swing_mean_reversion', name: 'bad-mode', parameters: { slope_mode: 'steep' } }), err => err.status !== 200 && /slope_mode must be one of/.test(err.message));
    const overview = await lab.overview('equity');
    const mr = overview.strategies.find(item => item.id === 'swing_mean_reversion');
    assert.ok(mr.tunable.includes('slope_mode') && mr.tunable.includes('regime_filter'));
});

test('a mean-reversion profile with filter settings applies to the settings file and is read back', async t => {
    const { root, meminfo } = fixtureRoot(t);
    const { loadSettings } = require('../server/algo-trader/settings');
    const lab = new ResearchLab({ root, python: '/venv/bin/python', meminfoPath: meminfo, checkProfiles: async () => {} });
    await lab.setPlan('equity', { swing_mean_reversion: 'high-risk' });
    await lab.equityPlanApply();
    const applied = (await loadSettings(root)).strategies.swing_mean_reversion;
    assert.equal(applied.slope_mode, 'volatility_scaled');
    assert.equal(applied.regime_filter, 'er');
    assert.equal(applied.efficiency_ratio_max, 0.5);
});

test('overview returns one strategies list per account; every equity strategy lists its three built-in profiles', async t => {
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
    assert.ok(equity.strategies.every(item => item.account === 'equity' && item.profiles.map(profile => profile.name).join(',') === 'standard,conservative,high-risk'));
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

test('an equity request needs at least one strategy; a list is a partial run; there is no full-run flag', () => {
    assert.throws(() => validateJobRequest({ account: 'equity' }, { today: TODAY }), /choose at least one strategy/);
    assert.throws(() => validateJobRequest({ account: 'equity', strategies: [] }, { today: TODAY }), /choose at least one strategy/);
    const some = validateJobRequest({ account: 'equity', strategies: ['swing_momentum', 'swing_momentum', 'intraday_trend_following'] }, { today: TODAY });
    assert.deepEqual(some.strategies, ['swing_momentum', 'intraday_trend_following']);
    assert.equal(some.full_run, undefined);
    assert.throws(() => validateJobRequest({ account: 'equity', strategies: ['made_up'] }, { today: TODAY }), /unknown equity strategies: made_up/);
});

test('an equity run waits for the nightly equity run and never starts beside it', async t => {
    const { root, meminfo } = fixtureRoot(t);
    const { spawnImpl, calls } = stubSpawn();
    const lab = new ResearchLab({ root, python: '/venv/bin/python', spawnImpl, meminfoPath: meminfo, nightlyRunning: async () => true });
    await assert.rejects(lab.startJob({ account: 'equity', strategies: ['swing_momentum'] }), error => error.status === 409 && /Nightly equity research/.test(error.message));
    assert.equal(calls.length, 0);
});

test('an equity run goes into its own output folder with the chosen strategies and no nightly flags', async t => {
    const { root, meminfo } = fixtureRoot(t);
    const { spawnImpl, calls } = stubSpawn();
    const lab = new ResearchLab({ root, python: '/venv/bin/python', spawnImpl, meminfoPath: meminfo, nightlyRunning: async () => false });
    const job = await lab.startJob({ account: 'equity', strategies: ['swing_momentum', 'intraday_trend_following'], no_refresh: true });
    const args = calls[0].args;
    assert.ok(args.includes('--lab-run'));
    assert.equal(args[args.indexOf('--strategies') + 1], 'swing_momentum,intraday_trend_following');
    assert.ok(!args.includes('--no-refresh'), 'equity runs always use the latest data');
    const outputDir = args[args.indexOf('--output-dir') + 1];
    assert.equal(outputDir, path.join(lab.jobDir(job.id), 'output'));
    assert.ok(!outputDir.includes(path.join(root, 'data', 'research', 'algo-auto')));
});


test('equity lab progress lines are read the same way as options progress', () => {
    const log = [
        'LAB_EVENT {"event": "started", "strategy": "equity", "total": 2, "underlyings": ["swing_momentum", "intraday_momentum"]}',
        'LAB_EVENT {"event": "underlying_started", "underlying": "swing_momentum", "index": 1, "total": 2}',
        'LAB_EVENT {"event": "underlying_done", "underlying": "swing_momentum", "status": "completed", "approved": false}',
        'LAB_EVENT {"event": "underlying_started", "underlying": "intraday_momentum", "index": 2, "total": 2}',
    ].join('\n');
    const progress = summarizeProgress(parseLabEvents(log));
    assert.equal(progress.total, 2);
    assert.equal(progress.done, 1);
    assert.equal(progress.current, 'intraday_momentum');
});

test('an equity run takes a universe of one or more tickers and dates, and passes them to the research command', async t => {
    const { root, meminfo } = fixtureRoot(t);
    const { spawnImpl, calls } = stubSpawn();
    const lab = new ResearchLab({ root, python: '/venv/bin/python', spawnImpl, meminfoPath: meminfo, nightlyRunning: async () => false });
    const tickers = Array.from({ length: 20 }, (_, index) => `t${String(index).padStart(2, '0')}`);
    const job = await lab.startJob({ account: 'equity', strategies: ['swing_momentum'], underlyings: tickers, start: '2023-01-01', end: '2026-06-30' });
    const args = calls[0].args;
    assert.equal(args[args.indexOf('--underlyings') + 1], tickers.map(item => item.toUpperCase()).join(','));
    assert.equal(args[args.indexOf('--start') + 1], '2023-01-01');
    assert.equal(args[args.indexOf('--end') + 1], '2026-06-30');
    assert.equal(job.underlyings.length, 20);
    assert.deepEqual(validateJobRequest({ account: 'equity', strategies: ['swing_momentum'], underlyings: ['spcx'] }, { today: TODAY }).underlyings, ['SPCX']);
    await assert.rejects(lab.startJob({ account: 'equity', strategies: ['swing_momentum'], underlyings: ['bad ticker'] }), /ticker symbol/);
    await assert.rejects(lab.startJob({ account: 'equity', strategies: ['swing_momentum'], start: '2024-05-01', end: '2024-05-01' }), /before end/);
});

test('equity profiles are fixed values: the same whatever the live settings are, so applying twice does not drift', () => {
    const { equityPresetParameters } = require('../server/algo-trader/research-lab');
    const presets = require('../server/algo-trader/profile-presets.json');
    const liveA = { max_position_pct: 0.07, risk_multiplier: 0.75, hard_stop_pct: 0.06, trailing_stop_pct: 0.045, trailing_activation_pct: 0.02 };
    const liveB = { max_position_pct: 0.049, risk_multiplier: 0.45, hard_stop_pct: 0.048, trailing_stop_pct: 0.036, trailing_activation_pct: 0.016 };
    for (const name of ['standard', 'conservative', 'high-risk']) {
        assert.deepEqual(equityPresetParameters(name, liveA, 'swing_momentum'), presets.equity.swing_momentum[name]);
        assert.deepEqual(equityPresetParameters(name, liveB, 'swing_momentum'), presets.equity.swing_momentum[name]);
    }
    assert.ok(presets.equity.swing_momentum.conservative.max_position_pct < presets.equity.swing_momentum.standard.max_position_pct);
    assert.ok(presets.equity.swing_momentum['high-risk'].max_position_pct > presets.equity.swing_momentum.standard.max_position_pct);
    assert.deepEqual(equityPresetParameters('high-risk', liveA, 'swing_multi_factor'), presets.equity.swing_multi_factor['high-risk']);
    assert.deepEqual(equityPresetParameters('high-risk', liveA, 'intraday_momentum'), presets.equity.intraday_momentum['high-risk']);
});
test('options profiles: the three built-ins come first and conservative needs a higher IV edge', async t => {
    const { root, meminfo } = fixtureRoot(t);
    const lab = new ResearchLab({ root, python: '/venv/bin/python', meminfoPath: meminfo });
    const profiles = await lab.listProfiles('vrp');
    assert.deepEqual(profiles.map(profile => profile.name), ['standard', 'conservative', 'high-risk']);
    const [standard, conservative, highRisk] = profiles;
    assert.equal(conservative.parameters.min_iv_minus_rv, Number((standard.parameters.min_iv_minus_rv * 1.5).toFixed(4)));
    assert.ok(highRisk.parameters.min_iv_minus_rv < standard.parameters.min_iv_minus_rv);
    await assert.rejects(lab.saveProfile({ strategy: 'vrp', name: 'standard', parameters: {} }), /built-in profile name/);
});

test('an equity run with a chosen profile gets a per-run override file; custom profiles are saved per strategy', async t => {
    const { root, meminfo } = fixtureRoot(t);
    const { spawnImpl, calls } = stubSpawn();
    const lab = new ResearchLab({ root, python: '/venv/bin/python', spawnImpl, meminfoPath: meminfo, nightlyRunning: async () => false, checkProfiles: async () => {} });
    await lab.saveProfile({ account: 'equity', strategy: 'swing_momentum', name: 'my-tight', parameters: { max_position_pct: 0.03 } });
    const job = await lab.startJob({ account: 'equity', strategies: ['swing_momentum'], profiles: { swing_momentum: 'my-tight' } });
    const overrides = JSON.parse(fs.readFileSync(path.join(lab.jobDir(job.id), 'overrides.json'), 'utf8'));
    assert.deepEqual(overrides, { swing_momentum: { max_position_pct: 0.03 } });
    assert.equal(calls[0].options.env.ALGO_LAB_PARAMETER_OVERRIDES, path.join(lab.jobDir(job.id), 'overrides.json'));
    await assert.rejects(lab.saveProfile({ account: 'equity', strategy: 'swing_momentum', name: 'standard', parameters: { max_position_pct: 0.03 } }), /built-in/);
    await assert.rejects(lab.saveProfile({ account: 'equity', strategy: 'swing_momentum', name: 'x', parameters: { made_up: 1 } }), /not a tunable/);
});

test('a profile value outside the strategy range is refused before the run starts', async t => {
    const { root, meminfo } = fixtureRoot(t);
    const { spawnImpl, calls } = stubSpawn();
    const lab = new ResearchLab({ root, python: '/venv/bin/python', spawnImpl, meminfoPath: meminfo, nightlyRunning: async () => false,
        checkProfiles: async overrides => { if (overrides.swing_momentum.max_position_pct > 0.05) throw new LabError('profile values are outside the strategy ranges. swing_momentum: too large', 422); } });
    await lab.saveProfile({ account: 'equity', strategy: 'swing_momentum', name: 'too-big', parameters: { max_position_pct: 0.09 } });
    await assert.rejects(lab.startJob({ account: 'equity', strategies: ['swing_momentum'], profiles: { swing_momentum: 'too-big' } }), error => error.status === 422);
    assert.equal(calls.length, 0);
});

test('an equity plan applies as one change with a diff and rolls back to the previous settings', async t => {
    const { root, meminfo } = fixtureRoot(t);
    const { loadSettings } = require('../server/algo-trader/settings');
    const lab = new ResearchLab({ root, python: '/venv/bin/python', meminfoPath: meminfo, checkProfiles: async () => {} });
    const before = (await loadSettings(root)).strategies.swing_momentum.max_position_pct;
    await lab.setPlan('equity', { swing_momentum: 'conservative' });
    const preview = await lab.equityPlanPreview();
    assert.ok(preview.changes > 0);
    assert.equal(preview.strategies[0].profile, 'conservative');
    await lab.equityPlanApply();
    const applied = (await loadSettings(root)).strategies.swing_momentum.max_position_pct;
    assert.ok(applied < before);
    assert.equal(applied, preview.strategies[0].parameters.max_position_pct);
    await lab.equityPlanRollback();
    assert.equal((await loadSettings(root)).strategies.swing_momentum.max_position_pct, before);
    await assert.rejects(lab.equityPlanRollback(), /no applied equity plan/);
});

test('rolling back an equity plan refuses when settings were edited after the apply, and force restores anyway', async t => {
    const { root, meminfo } = fixtureRoot(t);
    const { loadSettings } = require('../server/algo-trader/settings');
    const lab = new ResearchLab({ root, python: '/venv/bin/python', meminfoPath: meminfo, checkProfiles: async () => {} });
    const before = (await loadSettings(root)).strategies.swing_momentum.max_position_pct;
    await lab.setPlan('equity', { swing_momentum: 'conservative' });
    await lab.equityPlanApply();
    // A later hand edit to a stop-loss, written to the same override file the apply wrote.
    const overridePath = path.join(root, 'data', 'algo-strategy-config.json');
    const edited = JSON.parse(fs.readFileSync(overridePath, 'utf8'));
    edited.strategies.swing_momentum.hard_stop_pct = 0.05;
    fs.writeFileSync(overridePath, JSON.stringify(edited));
    await assert.rejects(lab.equityPlanRollback(), err => err.status === 409 && /settings changed after this apply: .*hard_stop_pct/.test(err.message));
    assert.equal((await loadSettings(root)).strategies.swing_momentum.hard_stop_pct, 0.05);
    await lab.equityPlanRollback({ force: true });
    assert.equal((await loadSettings(root)).strategies.swing_momentum.max_position_pct, before);
});

test('an options plan applies as one change to the options override and rolls back to the previous file', async t => {
    const { root, meminfo } = fixtureRoot(t);
    const { optionsConfigPath } = require('../server/algo-trader/settings');
    const lab = new ResearchLab({ root, python: '/venv/bin/python', meminfoPath: meminfo });
    const overridePath = optionsConfigPath(root);
    await lab.setPlan('options', { vrp: 'conservative' });
    const preview = await lab.planPreview('options');
    assert.ok(preview.changes > 0);
    assert.equal(preview.strategies[0].profile, 'conservative');
    await lab.planApply('options');
    const applied = JSON.parse(fs.readFileSync(overridePath, 'utf8'));
    assert.equal(applied.vrp.min_iv_minus_rv, preview.strategies[0].parameters.min_iv_minus_rv);
    await lab.planRollback('options');
    assert.equal(fs.existsSync(overridePath), false);
    await assert.rejects(lab.planRollback('options'), /no applied options plan/);
});

test('several options strategies run one after another, each with its own profile', async t => {
    const { root, meminfo } = fixtureRoot(t);
    const children = [];
    const spawnImpl = (executable, args, options) => {
        const child = new EventEmitter();
        child.pid = 4300 + children.length;
        child.kill = () => true;
        children.push({ child, args, options });
        return child;
    };
    const lab = new ResearchLab({ root, python: '/venv/bin/python', spawnImpl, meminfoPath: meminfo });
    await lab.startJob({ account: 'options', strategies: ['vrp', 'long_volatility'], underlyings: ['AAPL', 'MSFT'],
        profiles: { vrp: 'conservative', long_volatility: 'high-risk' } });
    assert.equal(children.length, 1);
    assert.deepEqual((await lab.overview('options')).queued, ['long_volatility']);
    children[0].child.emit('exit', 0, null);
    await new Promise(resolve => setTimeout(resolve, 50));
    assert.equal(children.length, 2);
    assert.equal(children[1].args[children[1].args.indexOf('--strategy') + 1], 'long_volatility');
    assert.deepEqual((await lab.overview('options')).queued, []);
});
