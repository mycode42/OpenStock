"""Small immutable options domain models shared by strategies and execution."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
import math
from typing import Literal


OptionRight = Literal["call", "put"]
LegSide = Literal["buy", "sell"]
PositionIntent = Literal["buy_to_open", "sell_to_open", "buy_to_close", "sell_to_close"]


def _finite(name: str, value: float, *, nonnegative: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{name} must be finite")
    if nonnegative and value < 0:
        raise ValueError(f"{name} must be non-negative")


@dataclass(frozen=True)
class OptionContract:
    symbol: str
    underlying: str
    expiration: date
    right: OptionRight
    strike: float
    multiplier: int = 100
    exercise_style: Literal["american", "european"] = "american"
    tradable: bool = True
    open_interest: int | None = None

    def __post_init__(self) -> None:
        if not self.symbol or self.symbol != self.symbol.upper():
            raise ValueError("contract symbol must be non-empty uppercase text")
        if not self.underlying or self.underlying != self.underlying.upper():
            raise ValueError("underlying must be non-empty uppercase text")
        if self.right not in {"call", "put"}:
            raise ValueError("right must be call or put")
        if self.exercise_style not in {"american", "european"}:
            raise ValueError("exercise_style must be american or european")
        _finite("strike", self.strike, nonnegative=True)
        if self.strike <= 0 or self.multiplier <= 0:
            raise ValueError("strike and multiplier must be positive")


@dataclass(frozen=True)
class GreeksSnapshot:
    """Per-share option Greeks; vega is USD per 1.00 absolute IV change.

    Portfolio aggregation multiplies these values by contract multiplier and
    signed contract count. Broker-provided units must be normalized by the
    adapter before constructing this model.
    """

    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    rho: float = 0.0
    implied_volatility: float = 0.0
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source: str = "unknown"

    def __post_init__(self) -> None:
        for name in ("delta", "gamma", "theta", "vega", "rho", "implied_volatility"):
            _finite(name, getattr(self, name))
        if self.implied_volatility < 0:
            raise ValueError("implied_volatility must be non-negative")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")


@dataclass(frozen=True)
class OptionLeg:
    contract: OptionContract
    side: LegSide
    ratio_qty: int = 1
    position_intent: PositionIntent | None = None
    bid: float = 0.0
    ask: float = 0.0
    greeks: GreeksSnapshot = field(default_factory=GreeksSnapshot)

    def __post_init__(self) -> None:
        if self.side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        if isinstance(self.ratio_qty, bool) or not isinstance(self.ratio_qty, int) or self.ratio_qty <= 0:
            raise ValueError("ratio_qty must be a positive integer")
        if self.position_intent is None:
            object.__setattr__(self, "position_intent", "buy_to_open" if self.side == "buy" else "sell_to_open")
        if self.position_intent not in {"buy_to_open", "sell_to_open", "buy_to_close", "sell_to_close"}:
            raise ValueError("unsupported position_intent")
        _finite("bid", self.bid, nonnegative=True)
        _finite("ask", self.ask, nonnegative=True)
        if self.bid and self.ask and self.ask < self.bid:
            raise ValueError("option quote is crossed")

    @property
    def signed_ratio(self) -> int:
        return self.ratio_qty if self.side == "buy" else -self.ratio_qty

    @property
    def midpoint(self) -> float:
        if self.bid <= 0 or self.ask <= 0:
            return 0.0
        return (self.bid + self.ask) / 2.0


@dataclass(frozen=True)
class RiskMetrics:
    max_loss: float
    max_profit: float | None
    estimated_margin: float
    premium_at_risk: float
    assignment_share_exposure: int = 0
    assignment_risk: str = "none"
    defined_risk: bool = True

    def __post_init__(self) -> None:
        _finite("max_loss", self.max_loss, nonnegative=True)
        _finite("estimated_margin", self.estimated_margin, nonnegative=True)
        _finite("premium_at_risk", self.premium_at_risk, nonnegative=True)
        if self.max_profit is not None:
            _finite("max_profit", self.max_profit, nonnegative=True)
        if isinstance(self.assignment_share_exposure, bool) or self.assignment_share_exposure < 0:
            raise ValueError("assignment_share_exposure must be non-negative")


@dataclass(frozen=True)
class OptionStructure:
    structure_id: str
    strategy: str
    structure_type: str
    underlying: str
    legs: tuple[OptionLeg, ...]
    net_debit_per_share: float
    greeks: GreeksSnapshot
    risk: RiskMetrics
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    event_id: str | None = None
    underlying_shares: int = 0
    underlying_cost_basis: float | None = None
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.structure_id or not self.strategy or not self.underlying:
            raise ValueError("structure_id, strategy, and underlying are required")
        if not self.legs or len(self.legs) > 4:
            raise ValueError("structures must contain one to four option legs")
        if any(leg.contract.underlying != self.underlying for leg in self.legs):
            raise ValueError("all legs must share the structure underlying")
        expirations = {leg.contract.expiration for leg in self.legs}
        if len(expirations) != 1:
            raise ValueError("this paper adapter supports same-expiration structures only")
        _finite("net_debit_per_share", self.net_debit_per_share)
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")

    @property
    def expiration(self) -> date:
        return self.legs[0].contract.expiration

@dataclass(frozen=True)
class OptionSignal:
    strategy: str
    underlying: str
    structure: OptionStructure
    desired_contracts: int
    confidence: float
    reason: str
    regime: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    event_id: str | None = None
    data_quality: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.desired_contracts <= 0:
            raise ValueError("desired_contracts must be positive")
        if not 0 <= float(self.confidence) <= 1:
            raise ValueError("confidence must be in [0, 1]")
        if not self.reason:
            raise ValueError("reason is required")

    @property
    def desired_positions(self) -> list[dict]:
        """Coordinator-facing target leg quantities and aggregate exposure."""
        positions = []
        for leg in self.structure.legs:
            signed_contracts = leg.signed_ratio * self.desired_contracts
            multiplier = leg.contract.multiplier
            # The paper runner supplies a fresh spot. Strike is only a
            # conservative display fallback for offline strategy construction.
            spot = float(self.structure.metadata.get("underlying_spot", leg.contract.strike) or leg.contract.strike)
            delta_shares = leg.greeks.delta * signed_contracts * multiplier
            positions.append({
                "symbol": leg.contract.symbol,
                "underlying": self.underlying,
                "signed_contracts": signed_contracts,
                "ratio_qty": leg.ratio_qty,
                "side": leg.side,
                "position_intent": leg.position_intent,
                "multiplier": multiplier,
                "strike": leg.contract.strike,
                "expiration": leg.contract.expiration.isoformat(),
                "right": leg.contract.right,
                "spot": spot,
                "notional": abs(signed_contracts) * multiplier * spot,
                "delta": delta_shares,
                "delta_notional": delta_shares * spot,
                "gamma": leg.greeks.gamma * signed_contracts * multiplier,
                "vega": leg.greeks.vega * signed_contracts * multiplier,
                "theta": leg.greeks.theta * signed_contracts * multiplier,
            })
        return positions

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "underlying": self.underlying,
            "structure_id": self.structure.structure_id,
            "structure_type": self.structure.structure_type,
            "desired_contracts": self.desired_contracts,
            "desired_positions": self.desired_positions,
            "confidence": self.confidence,
            "reason": self.reason,
            "regime": self.regime,
            "event_id": self.event_id,
            "net_debit_per_share": self.structure.net_debit_per_share,
            "greeks": asdict(self.structure.greeks),
            "risk": asdict(self.structure.risk),
            "created_at": self.created_at.isoformat(),
            "data_quality": dict(self.data_quality),
        }


@dataclass(frozen=True)
class OptionPosition:
    account_id: str
    structure_id: str
    underlying: str
    legs: tuple[OptionLeg, ...]
    quantity: int
    average_open_price: float
    market_value: float
    greeks: GreeksSnapshot
    risk: RiskMetrics
    opened_at: datetime | None = None
    last_marked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0


@dataclass(frozen=True)
class OptionOrder:
    account_id: str
    client_order_id: str
    underlying: str
    structure_id: str
    structure_type: str
    legs: tuple[OptionLeg, ...]
    contracts: int
    order_type: Literal["limit", "market"]
    limit_price: float | None
    time_in_force: Literal["day", "gtc"] = "day"
    status: str = "pending"
    broker_order_id: str | None = None
    submitted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if not self.account_id or not self.client_order_id or not self.legs:
            raise ValueError("option order account, id, and legs are required")
        if isinstance(self.contracts, bool) or not isinstance(self.contracts, int) or self.contracts <= 0:
            raise ValueError("option order contracts must be a positive integer")
        if self.order_type == "limit" and (self.limit_price is None or not math.isfinite(self.limit_price)):
            raise ValueError("limit orders require a finite limit_price")
        if self.time_in_force not in {"day", "gtc"}:
            raise ValueError("options time_in_force must be day or gtc")


@dataclass(frozen=True)
class OptionFill:
    broker_order_id: str
    client_order_id: str
    contract_symbol: str
    side: LegSide
    contracts: int
    fill_price: float
    commission: float
    filled_at: datetime
    greeks_at_fill: GreeksSnapshot

    def __post_init__(self) -> None:
        if self.contracts <= 0 or self.fill_price < 0 or self.commission < 0:
            raise ValueError("option fill quantity, price, and commission are invalid")
        if self.filled_at.tzinfo is None or self.filled_at.utcoffset() is None:
            raise ValueError("filled_at must be timezone-aware")


@dataclass(frozen=True)
class PortfolioRiskSummary:
    equity: float
    options_equity: float
    delta_by_underlying: dict[str, float]
    delta_notional_by_underlying: dict[str, float]
    gamma_pnl_1pct_by_underlying: dict[str, float]
    vega_per_vol_point_by_underlying: dict[str, float]
    theta_per_day_by_underlying: dict[str, float]
    max_loss_at_risk: float
    estimated_margin: float
    assignment_shares: int
    stress_loss_5pct: float
    stress_loss_10pct: float
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    data_quality: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        row = asdict(self)
        row["observed_at"] = self.observed_at.isoformat()
        row["data_quality"] = list(self.data_quality)
        return row
