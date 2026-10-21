const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { createHash } = require('node:crypto');
const registerAlgoTraderRoutes = require('../server/routes/algo-trader');

const optionsDefaults = {
  enabled: true,
  market_data_feed: 'opra',
  max_quote_age_seconds: 120,
  max_quote_spread_pct: 0.25,
  min_open_interest: 100,
  rate: 0.04,
  dividend_yield: 0.015,
  assignment_extrinsic_threshold: 0.02,
  dividends: {},
  regime_risk_multipliers: { range_bound: 1, trending_up: 0.75, trending_down: 0.5, unknown: 0 },
  costs: { commission_per_contract_usd: 0.65, slippage_per_contract_usd: 5 },
  risk: {
    max_risk_per_trade_pct: 0.005, max_total_defined_risk_pct: 0.1, max_open_structures: 3,
    max_entries_per_day: 2, max_daily_loss_pct: 0.02, max_contracts_per_trade: 1,
    max_delta_notional_pct: 0.75, max_vega_per_vol_point_pct: 0.01,
    max_gamma_1pct_loss_pct: 0.005, max_theta_loss_per_day_pct: 0.001,
    max_assignment_shares: 200, max_margin_pct: 0.15, stress_spot_shocks: [0.05, 0.1], stress_iv_shock: 0.1,
  },
  vrp: {
    enabled: true, underlying: 'SPY', min_dte: 25, max_dte: 45, min_iv_minus_rv: 0.04,
    realized_lookback_sessions: 20, short_delta_min: 0.1, short_delta_max: 0.2,
    wing_delta_max: 0.07, allowed_regimes: ['range_bound', 'trending_up', 'trending_down'], max_entries_per_day: 1,
  },
  event_driven: {
    enabled: true, min_days_before_event: 0, max_days_before_event: 5, min_historical_events: 3,
    min_move_edge_pct: 0.15, min_llm_confidence: 0.6, min_dte: 3, max_dte: 35,
    max_spread_pct: 0.2, realized_lookback_days: 730,
  },
  directional_overlay: {
    enabled: false, underlying: 'SPY', min_dte: 25, max_dte: 60, target_dte: 40,
    long_delta: 0.5, short_delta: 0.25, min_signal_confidence: 0.6, max_spread_pct: 0.2, min_open_interest: 100,
  },
};

function routeHandlers(root) {
  const handlers = {};
  const app = {
    get: (route, handler) => { handlers[`GET ${route}`] = handler; },
    put: (route, handler) => { handlers[`PUT ${route}`] = handler; },
  };
  registerAlgoTraderRoutes({ app, root });
  return handlers;
}

function response() {
  return {
    statusCode: 200,
    status(code) { this.statusCode = code; return this; },
    json(body) { this.body = body; return this; },
  };
}

async function fixture(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'openstock-algo-test-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  await fs.mkdir(path.join(root, 'options'), { recursive: true });
  await fs.writeFile(path.join(root, 'options', 'config.default.json'), JSON.stringify(optionsDefaults));
  return root;
}

test('options paper account payload reads its own account journal and research', async t => {
  const originalEnv = {
    account: process.env.ALPACA_OPTIONS_ACCOUNT_ID,
    config: process.env.ALPACA_OPTIONS_CONFIG,
    feed: process.env.ALPACA_OPTIONS_FEED,
  };
  delete process.env.ALPACA_OPTIONS_ACCOUNT_ID;
  delete process.env.ALPACA_OPTIONS_CONFIG;
  delete process.env.ALPACA_OPTIONS_FEED;
  t.after(() => {
    for (const [key, value] of Object.entries({
      ALPACA_OPTIONS_ACCOUNT_ID: originalEnv.account,
      ALPACA_OPTIONS_CONFIG: originalEnv.config,
      ALPACA_OPTIONS_FEED: originalEnv.feed,
    })) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  });

  const root = await fixture(t);
  const accountDigest = createHash('sha256').update('test-options-account').digest('hex').slice(0, 16);
  const accountDir = path.join(root, 'data', 'options', 'accounts', accountDigest);
  const researchDir = path.join(root, 'data', 'options', 'research', 'automation');
  await fs.mkdir(accountDir, { recursive: true });
  await fs.mkdir(researchDir, { recursive: true });
  await fs.writeFile(path.join(accountDir, 'risk_daily.jsonl'), [
    JSON.stringify({ logged_at: '2026-10-19T20:00:00Z', options_equity: 10000, open_option_positions: [] }),
    JSON.stringify({ logged_at: '2026-10-20T20:00:00Z', options_equity: 11000, open_option_positions: [{ symbol: 'SPY' }], regime: 'range_bound' }),
  ].join('\n'));
  await fs.writeFile(path.join(accountDir, 'decisions.jsonl'), JSON.stringify({
    logged_at: '2026-10-20T20:00:00Z', approved: true, reason: 'risk limits passed',
    signal: { strategy: 'vrp', underlying: 'SPY' },
  }));
  await fs.writeFile(path.join(researchDir, 'latest.json'), JSON.stringify({
    status: 'completed', run_date_et: '2026-10-20', created_at: '2026-10-20T21:00:00Z',
    strategies: { vrp: { status: 'completed', approved: true, underlying: 'SPY' } },
  }));

  const handlers = routeHandlers(root);
  const res = response();
  await handlers['GET /api/algo-trader']({ query: { account: 'options' } }, res);

  assert.equal(res.statusCode, 200);
  assert.equal(res.body.account_type, 'options');
  assert.equal(res.body.account.mode, 'paper');
  assert.equal(res.body.performance.metrics.equity, 11000);
  assert.equal(res.body.performance.metrics.daily_pnl, 1000);
  assert.deepEqual(res.body.account.active_strategies, ['vrp']);
  assert.equal(res.body.research.latest.promoted, 1);
  assert.ok(res.body.activity.some(item => item.title.includes('SPY')));
  assert.equal(Object.hasOwn(res.body.account, 'account_id'), false);
});

test('equity research payload includes the latest optimizer cycle and strategy discovery results', async t => {
  const root = await fixture(t);
  const dataDir = path.join(root, 'data');
  const researchDir = path.join(dataDir, 'research', 'algo-auto', 'optimizer');
  await fs.mkdir(researchDir, { recursive: true });
  await fs.writeFile(path.join(dataDir, 'algo_state.json'), JSON.stringify({
    strategy_portfolio: { account_id: 'test-account', universe: { tickers: [] } },
  }));
  await fs.writeFile(path.join(researchDir, 'latest.json'), JSON.stringify({
    invocation_id: 'run123', status: 'completed', run_date_et: '2026-10-21',
    created_at: '2026-10-21T15:09:36.925Z',
    data: { strategy_research_range: { start: '2024-01-01', end: '2026-10-19' } },
    stage_timings: { total_seconds: 1574.2 },
    adaptive_research: { discovery: { strategies: {
      mean_reversion: {
        status: 'completed', archive_sequence: 28, search_iterations: 5,
        adaptive_novelty: { new_parameter_variants_on_this_data: 12 },
        selection_status: 'eligible_positive_candidate',
        selected_candidate: { name: 'iteration_02:bayes_011', eligible: true, metrics: { net_return_pct: 0.0153, closed_trade_count: 4 } },
        window_search: { attempts: [
          { window_days: 10, selected_candidate: 'candidate_a', selected_fitness: -0.1, fitness_delta: 0.05 },
          { window_days: 20, selected_candidate: 'iteration_02:bayes_011', selected_fitness: 0.5244, fitness_delta: 0.6744 },
        ] },
      },
    } } },
  }));

  const handlers = routeHandlers(root);
  const res = response();
  await handlers['GET /api/algo-trader']({ query: { account: 'equity' } }, res);

  assert.equal(res.statusCode, 200);
  assert.equal(res.body.research.optimizer.run_id, 'optimization-run123');
  assert.equal(res.body.research.optimizer.range_start, '2024-01-01');
  assert.equal(res.body.research.optimizer.new_variants, 12);
  assert.equal(res.body.research.optimizer.archive_sequence, 28);
  assert.equal(res.body.research.optimizer.strategies[0].selected_candidate, 'iteration_02:bayes_011');
  assert.equal(res.body.research.optimizer.strategies[0].top_tested_fitness, 0.5244);
  assert.equal(res.body.research.optimizer.strategies[0].top_tested_delta, 0.6744);
});

test('options settings save to their own config without changing equity overrides', async t => {
  const originalEnv = {
    account: process.env.ALPACA_OPTIONS_ACCOUNT_ID,
    config: process.env.ALPACA_OPTIONS_CONFIG,
    feed: process.env.ALPACA_OPTIONS_FEED,
  };
  delete process.env.ALPACA_OPTIONS_ACCOUNT_ID;
  delete process.env.ALPACA_OPTIONS_CONFIG;
  delete process.env.ALPACA_OPTIONS_FEED;
  t.after(() => {
    for (const [key, value] of Object.entries({
      ALPACA_OPTIONS_ACCOUNT_ID: originalEnv.account,
      ALPACA_OPTIONS_CONFIG: originalEnv.config,
      ALPACA_OPTIONS_FEED: originalEnv.feed,
    })) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  });

  const root = await fixture(t);
  const handlers = routeHandlers(root);
  const res = response();
  await handlers['PUT /api/algo-trader/config']({
    query: { account: 'options' }, body: { settings: { ...optionsDefaults, enabled: false } },
  }, res);

  assert.equal(res.statusCode, 200);
  assert.equal(res.body.settings.enabled, false);
  assert.equal(JSON.parse(await fs.readFile(path.join(root, 'data', 'options', 'config.json'), 'utf8')).enabled, false);
  await assert.rejects(fs.access(path.join(root, 'data', 'dashboard-runtime-overrides.json')));
});
