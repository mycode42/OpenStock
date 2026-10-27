"""Cross-asset Greek, defined-loss, buying-power, and stress risk controls."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from options.costs import OptionsCostModel
from options.models import GreeksSnapshot, OptionSignal, OptionStructure, PortfolioRiskSummary
from options.structures import structure_limit_price
from runtime.atomic import atomic_write_json
from runtime.paths import DATA_DIR


RISK_SNAPSHOT_PATH = DATA_DIR / "options" / "risk_snapshot.json"


@dataclass(frozen=True)
class PositionExposure:
    underlying: str
    spot: float
    delta_shares: float
    gamma: float = 0.0
    theta_per_day: float = 0.0
    vega_per_volatility: float = 0.0
    max_loss: float = 0.0
    margin: float = 0.0
    assignment_shares: int = 0
    # Shares of the underlying committed to short calls (contracts x multiplier).
    short_call_shares: int = 0
    source: str = "unknown"


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    contracts: int
    reason: str | None
    risk_per_structure: float
    total_risk: float
    margin_after: float
    order_limit_price: float
    projected_risk: dict


def _safe_number(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def summarize_portfolio(
    *,
    combined_equity: float,
    options_equity: float,
    positions: Iterable[PositionExposure],
    data_quality: Iterable[str] = (),
    stress_spot_shocks: Sequence[float],
    stress_iv_shock: float,
) -> PortfolioRiskSummary:
    shocks = tuple(float(value) for value in stress_spot_shocks)
    if len(shocks) != 2:
        raise ValueError("stress_spot_shocks must contain exactly two shocks")
    if not math.isfinite(combined_equity) or combined_equity <= 0:
        raise ValueError("combined portfolio equity must be positive")
    if not math.isfinite(options_equity) or options_equity <= 0:
        raise ValueError("options account equity must be positive")
    delta: dict[str, float] = {}
    gamma: dict[str, float] = {}
    vega_point: dict[str, float] = {}
    theta: dict[str, float] = {}
    max_loss = margin = 0.0
    assignment_shares = 0
    rows = list(positions)
    for row in rows:
        symbol = row.underlying.upper()
        if row.spot <= 0 or not math.isfinite(row.spot):
            raise ValueError(f"invalid underlying spot for {symbol}")
        delta[symbol] = delta.get(symbol, 0.0) + row.delta_shares
        gamma[symbol] = gamma.get(symbol, 0.0) + row.gamma
        vega_point[symbol] = vega_point.get(symbol, 0.0) + row.vega_per_volatility * 0.01
        theta[symbol] = theta.get(symbol, 0.0) + row.theta_per_day
        max_loss += max(0.0, row.max_loss)
        margin += max(0.0, row.margin)
        assignment_shares += abs(int(row.assignment_shares))
    delta_notional = {symbol: shares * next(row.spot for row in rows if row.underlying.upper() == symbol) for symbol, shares in delta.items()}
    gamma_pnl = {
        symbol: 0.5 * value * (next(row.spot for row in rows if row.underlying.upper() == symbol) * 0.01) ** 2
        for symbol, value in gamma.items()
    }
    stress_results: list[float] = []
    for shock in shocks:
        scenario_pnls = []
        for direction in (-1, 1):
            pnl = 0.0
            for row in rows:
                move = direction * shock * row.spot
                pnl += row.delta_shares * move + 0.5 * row.gamma * move * move + row.theta_per_day
                # Volatility shocks are applied adversely to each position's vega.
                pnl -= abs(row.vega_per_volatility) * stress_iv_shock
            scenario_pnls.append(pnl)
        stress_results.append(max(0.0, -min(scenario_pnls)))
    return PortfolioRiskSummary(
        equity=combined_equity,
        options_equity=options_equity,
        delta_by_underlying=delta,
        delta_notional_by_underlying=delta_notional,
        gamma_pnl_1pct_by_underlying=gamma_pnl,
        vega_per_vol_point_by_underlying=vega_point,
        theta_per_day_by_underlying=theta,
        max_loss_at_risk=max_loss,
        estimated_margin=margin,
        assignment_shares=assignment_shares,
        # Field names are legacy labels: they hold the first and second configured shocks.
        stress_loss_5pct=stress_results[0],
        stress_loss_10pct=stress_results[1],
        data_quality=tuple(sorted(set(data_quality))),
    )


class PortfolioRiskManager:
    """Checks an options proposal against both equity and option holdings.

    Equity positions are supplied by the Algo paper account; option holdings and
    buying power come from the separate options paper account. Broker buying
    power remains the final authority for order acceptance.
    """

    def __init__(self, config: dict, cost_model: OptionsCostModel | None = None) -> None:
        self.config = dict(config)
        self.costs = cost_model or OptionsCostModel()

    def size_and_validate(
        self,
        signal: OptionSignal,
        *,
        combined_equity: float,
        options_equity: float,
        equity_positions: Iterable[PositionExposure],
        options_equity_positions: Iterable[PositionExposure] = (),
        option_positions: Iterable[PositionExposure],
        existing_option_structures: int,
        options_buying_power: float | None = None,
    ) -> RiskDecision:
        limits = self.config
        options_equity_positions = list(options_equity_positions)
        option_positions = list(option_positions)
        base = list(equity_positions) + options_equity_positions + option_positions
        stress_kwargs = {
            "stress_spot_shocks": limits["stress_spot_shocks"],
            "stress_iv_shock": float(limits["stress_iv_shock"]),
        }
        current = summarize_portfolio(
            combined_equity=combined_equity,
            options_equity=options_equity,
            positions=base,
            **stress_kwargs,
        )
        structure = signal.structure
        if not structure.risk.defined_risk or not math.isfinite(structure.risk.max_loss):
            return self._reject("undefined-loss structures are disabled", signal)
        covered_structure_units: int | None = None
        if structure.structure_type in {"protective_put", "collar"}:
            if structure.underlying_shares <= 0:
                return self._reject("covered structure is missing its required share quantity", signal)
            held_shares = sum(
                max(0.0, position.delta_shares)
                for position in options_equity_positions
                if position.underlying == structure.underlying
            )
            # Shares already pledged to open short calls (including collars) cannot cover a new call.
            committed_shares = sum(
                position.short_call_shares
                for position in option_positions
                if position.underlying == structure.underlying
            )
            covered_shares = held_shares - committed_shares
            if covered_shares < structure.underlying_shares:
                return self._reject("shares required to cover this structure are not held in the options account", signal)
            covered_structure_units = int(covered_shares // structure.underlying_shares)
        if _safe_number(structure.metadata.get("underlying_spot")) <= 0:
            return self._reject("fresh underlying spot is required for cross-asset risk", signal)
        if not structure.legs or any(not leg.contract.tradable for leg in structure.legs):
            return self._reject("synthetic or non-tradable contracts cannot be sent to the broker", signal)
        if existing_option_structures >= int(limits.get("max_open_structures", 3)):
            return self._reject("options open-structure limit reached", signal)

        costs_one = self.costs.estimate(structure, 1)
        risk_per_unit = structure.risk.max_loss + costs_one
        if risk_per_unit <= 0 or not math.isfinite(risk_per_unit):
            return self._reject("structure has invalid defined loss", signal)
        regime_weight = _safe_number(signal.data_quality.get("regime_risk_multiplier"), 1.0)
        if regime_weight <= 0 or regime_weight > 2.0:
            return self._reject("signal regime risk weight disables this entry", signal)
        max_risk_pct = float(limits.get("max_risk_per_trade_pct", 0.005))
        strategy_risk_pct = signal.data_quality.get("max_risk_per_trade_pct")
        if strategy_risk_pct is not None:
            strategy_risk_pct = _safe_number(strategy_risk_pct, 0.0)
            if strategy_risk_pct <= 0 or strategy_risk_pct > 1:
                return self._reject("strategy-specific premium risk budget is invalid", signal)
            max_risk_pct = min(max_risk_pct, strategy_risk_pct)
        trade_budget = options_equity * max_risk_pct * regime_weight
        max_by_loss = int(trade_budget // risk_per_unit)
        max_by_premium = signal.desired_contracts
        premium_cap = signal.data_quality.get("max_premium_per_trade_usd")
        if premium_cap is not None:
            premium_cap = _safe_number(premium_cap, 0.0)
            premium_per_structure = structure.risk.premium_at_risk
            if premium_cap <= 0 or premium_per_structure <= 0:
                return self._reject("strategy-specific premium cap is invalid", signal)
            max_by_premium = int(premium_cap // premium_per_structure)
        contracts = min(
            signal.desired_contracts,
            int(limits.get("max_contracts_per_trade", 1)),
            max_by_loss,
            max_by_premium,
            covered_structure_units if covered_structure_units is not None else signal.desired_contracts,
        )
        if contracts <= 0:
            return self._reject("premium risk plus costs exceeds the per-trade budget", signal)
        total_risk = structure.risk.max_loss * contracts + self.costs.estimate(structure, contracts)
        if current.max_loss_at_risk + total_risk > options_equity * float(limits.get("max_total_defined_risk_pct", 0.10)):
            return self._reject("combined defined-loss budget would be exceeded", signal)
        margin_after = current.estimated_margin + structure.risk.estimated_margin * contracts
        margin_cap = options_equity * float(limits.get("max_margin_pct", 0.15))
        if margin_after > margin_cap:
            return self._reject("options margin limit would be exceeded", signal)
        if options_buying_power is not None and structure.risk.estimated_margin * contracts > options_buying_power:
            return self._reject("broker options buying power is insufficient", signal)

        candidate = self._candidate_exposure(structure, contracts)
        projected = summarize_portfolio(
            combined_equity=combined_equity,
            options_equity=options_equity,
            positions=[*base, candidate],
            **stress_kwargs,
        )
        if max((abs(v) for v in projected.delta_notional_by_underlying.values()), default=0.0) > combined_equity * float(limits.get("max_delta_notional_pct", 0.75)):
            return self._reject("combined per-underlying delta-notional limit would be exceeded", signal)
        total_vega = sum(abs(value) for value in projected.vega_per_vol_point_by_underlying.values())
        if total_vega > combined_equity * float(limits.get("max_vega_per_vol_point_pct", 0.01)):
            return self._reject("combined vega limit would be exceeded", signal)
        total_gamma_loss = sum(abs(value) for value in projected.gamma_pnl_1pct_by_underlying.values())
        if total_gamma_loss > combined_equity * float(limits.get("max_gamma_1pct_loss_pct", 0.005)):
            return self._reject("combined gamma limit would be exceeded", signal)
        theta_loss_per_day = sum(
            max(0.0, -value) for value in projected.theta_per_day_by_underlying.values()
        )
        if theta_loss_per_day > combined_equity * float(limits.get("max_theta_loss_per_day_pct", 0.001)):
            return self._reject("combined daily theta-loss limit would be exceeded", signal)
        if projected.assignment_shares > int(limits.get("max_assignment_shares", 200)):
            return self._reject("early-assignment share exposure limit would be exceeded", signal)
        if projected.stress_loss_10pct > combined_equity * float(limits["stress_gate_loss_pct"]):
            return self._reject("combined underlying stress loss exceeds the configured equity limit", signal)

        order_price = structure_limit_price(structure, costs=self.costs, contracts=contracts)
        # A credit structure must remain a credit after conservative slippage.
        # Crossing zero would silently turn a credit entry into a net debit.
        if structure.net_debit_per_share < 0 and order_price >= 0:
            return self._reject("estimated slippage consumes the entire credit", signal)
        if structure.net_debit_per_share > 0 and order_price <= 0:
            return self._reject("estimated slippage leaves no valid debit limit", signal)
        return RiskDecision(
            approved=True,
            contracts=contracts,
            reason=None,
            risk_per_structure=risk_per_unit,
            total_risk=total_risk,
            margin_after=margin_after,
            order_limit_price=order_price,
            projected_risk=projected.to_dict(),
        )

    @staticmethod
    def _candidate_exposure(structure: OptionStructure, contracts: int) -> PositionExposure:
        legs = structure.legs
        spot = float(structure.metadata.get("underlying_spot", 0) or 0)
        if spot <= 0:
            spot = float(structure.metadata.get("spot", 0) or 0)
        # The spot is supplied by paper runner before risk evaluation.
        if spot <= 0:
            spot = float(structure.metadata.get("reference_price", 0) or 0)
        short_call_shares = sum(
            leg.ratio_qty * leg.contract.multiplier
            for leg in legs if leg.side == "sell" and leg.contract.right == "call"
        ) * contracts
        return PositionExposure(
            underlying=structure.underlying,
            spot=spot,
            delta_shares=structure.greeks.delta * contracts,
            gamma=structure.greeks.gamma * contracts,
            theta_per_day=structure.greeks.theta * contracts,
            vega_per_volatility=structure.greeks.vega * contracts,
            max_loss=structure.risk.max_loss * contracts,
            margin=structure.risk.estimated_margin * contracts,
            assignment_shares=structure.risk.assignment_share_exposure * contracts,
            short_call_shares=int(short_call_shares),
            source="options_proposal",
        )

    @staticmethod
    def _reject(reason: str, signal: OptionSignal) -> RiskDecision:
        return RiskDecision(False, 0, reason, signal.structure.risk.max_loss, 0.0, 0.0, 0.0, {})


def pnl_attribution(
    *,
    previous_greeks: GreeksSnapshot,
    underlying_move: float,
    implied_volatility_move: float,
    days_elapsed: float,
    multiplier: int = 100,
    contracts: int = 1,
    realized_pnl: float,
) -> dict[str, float]:
    """First/second-order attribution; residual captures higher-order effects."""
    scale = multiplier * contracts
    delta = previous_greeks.delta * underlying_move * scale
    gamma = 0.5 * previous_greeks.gamma * underlying_move**2 * scale
    vega = previous_greeks.vega * implied_volatility_move * scale
    theta = previous_greeks.theta * days_elapsed * scale
    modeled = delta + gamma + vega + theta
    return {
        "delta": delta,
        "gamma": gamma,
        "vega": vega,
        "theta": theta,
        "residual": realized_pnl - modeled,
        "realized_pnl": realized_pnl,
    }


def publish_risk_snapshot(
    summary: PortfolioRiskSummary,
    *,
    account_id: str,
    snapshot_path: Path = RISK_SNAPSHOT_PATH,
) -> None:
    """Publish option-only delta notional plus audit metadata for equity scans."""
    atomic_write_json(snapshot_path, {
        "schema_version": 1,
        "account_id": account_id,
        "snapshot_at": summary.observed_at.isoformat(),
        "options_delta_notional_by_underlying": summary.delta_notional_by_underlying,
        "summary": summary.to_dict(),
    })


def load_options_delta_overlay(
    snapshot_path: Path = RISK_SNAPSHOT_PATH,
    *,
    max_age_seconds: int = 86400,
) -> tuple[dict[str, float], dict]:
    """Read last-published options delta exposure; retain stale values conservatively."""
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
        stamp = datetime.fromisoformat(str(payload["snapshot_at"]).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("options risk snapshot has a naive timestamp")
        age = (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds()
        mapping = payload.get("options_delta_notional_by_underlying") or {}
        values = {
            str(key).upper(): _safe_number(value)
            for key, value in mapping.items()
            if str(key) and math.isfinite(_safe_number(value)) and _safe_number(value) != 0
        }
        # Keep stale exposure in the coordinator until a newer account snapshot
        # replaces it. This can overstate risk after an external manual close.
        return values, {
            "available": True,
            "age_seconds": max(0.0, age),
            "stale": age > max_age_seconds,
            "account_id": payload.get("account_id"),
            "source": str(snapshot_path),
        }
    except FileNotFoundError:
        return {}, {"available": False, "stale": True, "reason": "options risk snapshot has not been published"}
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return {}, {"available": False, "stale": True, "reason": f"invalid options risk snapshot: {type(exc).__name__}"}


def update_summary_snapshot(summary: PortfolioRiskSummary, *, account_id: str, path: Path = RISK_SNAPSHOT_PATH) -> None:
    """Compatibility alias for callers that explicitly name summary snapshots."""
    publish_risk_snapshot(summary, account_id=account_id, snapshot_path=path)


__all__ = [
    "PositionExposure", "RiskDecision", "PortfolioRiskManager", "summarize_portfolio",
    "pnl_attribution", "publish_risk_snapshot", "load_options_delta_overlay",
]
