# Long volatility options sleeve

The `long_volatility` sleeve is enabled by default in `options/config.default.json` and proposes new trades only in the `volatile` regime. Set `mode` to `exclusive` to suppress the other options sleeves; `alongside` lets the existing VRP and event sleeves compete for shared account risk. All opens and closes use the options paper account and its multi-leg order route.

```json
{
  "long_volatility": {
    "enabled": true,
    "mode": "alongside",
    "underlyings": ["SPY"],
    "allowed_regimes": ["volatile"],
    "structure": "strangle",
    "min_dte": 7,
    "max_dte": 45,
    "target_dte": 30,
    "otm_delta": 0.25,
    "iv_percentile_lookback_sessions": 252,
    "iv_rank_min_samples": 20,
    "bootstrap_iv_history_from_cboe": true,
    "iv_percentile_ceiling": 75,
    "max_risk_per_trade_pct": 0.005,
    "max_spread_pct": 0.15,
    "min_open_interest": 100,
    "max_premium_per_trade_usd": 1000,
    "profit_target_pct": 0.40,
    "stop_loss_pct": 0.50,
    "close_dte": 3,
    "max_holding_days": 14,
    "close_on_regime_exit": true
  }
}
```

Paper mode calculates the IV percentile from prior daily liquid ATM call/put IV observations stored per account in the options journal. When `bootstrap_iv_history_from_cboe` is enabled, the first cycle imports missing prior sessions from the local `data/options/research/cboe_<underlying>_eod` archive, then live Alpaca observations extend the series. Run `poetry run python -m options.iv_history` to seed the account journal manually; the import is idempotent. Its source is recorded in the IV history and signal diagnostics. Fewer than `iv_rank_min_samples` prior sessions still suppresses signals. Historical Cboe IV and live Alpaca IV can differ slightly by calculation source, so the source list is retained for analysis. A research dataset with point-in-time chains supplies the same rank calculation from prior observations only. The research runner accepts `long_volatility` and includes it in the normal purged walk-forward, slippage, Greek, regime, and holdout validation flow.

The default long strangle limits loss to the premium paid plus fees. Risk is still capped by the options risk manager's per-trade budget and aggregate delta, gamma, vega, theta, margin, and stress limits. Long premium can lose value through IV crush, theta decay, and overnight gaps; a stop or regime-triggered exit is a paper limit order and may not fill immediately.
