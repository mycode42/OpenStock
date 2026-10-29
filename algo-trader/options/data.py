"""Option-chain provider interfaces for live paper data and backtest replay."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import cached_property, lru_cache
import math
from typing import Callable, Iterable, Mapping, Protocol, Sequence

from market.quotes import QuoteParseError, parse_quote
from options.market_inputs import MarketInputs
from options.models import GreeksSnapshot, OptionContract, OptionLeg
from options.pricing import implied_volatility, model_greeks, years_to_expiration


from options.models import _compact_reduce  # noqa: E402  (shared compact pickle form)


@dataclass(frozen=True)
class ChainQuote:
    contract: OptionContract
    bid: float
    ask: float
    greeks: GreeksSnapshot
    observed_at: datetime
    feed: str

    @property
    def midpoint(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_pct(self) -> float:
        return (self.ask - self.bid) / self.midpoint if self.midpoint > 0 else math.inf

    def as_leg(self, side: str, *, ratio_qty: int = 1) -> OptionLeg:
        return OptionLeg(self.contract, side, ratio_qty, bid=self.bid, ask=self.ask, greeks=self.greeks)


@dataclass(frozen=True)
class OptionChain:
    underlying: str
    spot: float
    as_of: datetime
    contracts: tuple[ChainQuote, ...]
    feed: str
    data_quality: tuple[str, ...] = ()

    @cached_property
    def _expiration_dtes(self) -> tuple[tuple[int, date], ...]:
        today = self.as_of.date()
        return tuple(sorted({
            ((row.contract.expiration - today).days, row.contract.expiration)
            for row in self.contracts
        }))

    @cached_property
    def _expirations_by_range(self) -> dict[tuple[int, int], tuple[date, ...]]:
        return {}

    @cached_property
    def _quotes_by_expiration(self) -> dict[date, tuple[ChainQuote, ...]]:
        grouped: dict[date, list[ChainQuote]] = {}
        for row in self.contracts:
            grouped.setdefault(row.contract.expiration, []).append(row)
        return {expiration: tuple(rows) for expiration, rows in grouped.items()}

    @cached_property
    def _symbol_index(self) -> Mapping[str, ChainQuote]:
        return {row.contract.symbol: row for row in self.contracts}

    @cached_property
    def _derived_cache(self) -> dict[tuple, object]:
        return {}

    def expirations(self, *, min_dte: int = 0, max_dte: int = 365) -> tuple[date, ...]:
        key = (min_dte, max_dte)
        cached = self._expirations_by_range.get(key)
        if cached is None:
            cached = tuple(
                expiration for dte, expiration in self._expiration_dtes
                if min_dte <= dte <= max_dte
            )
            self._expirations_by_range[key] = cached
        return cached

    def for_expiration(self, expiration: date) -> tuple[ChainQuote, ...]:
        return self._quotes_by_expiration.get(expiration, ())

    def symbol_index(self) -> Mapping[str, ChainQuote]:
        """Read-only cached symbol lookup for repeated simulation access."""
        return self._symbol_index

    def cached_derived(self, key: tuple, factory: Callable[[], object]):
        """Cache immutable derived views used repeatedly during one research run."""
        cache = self._derived_cache
        if key not in cache:
            cache[key] = factory()
        return cache[key]

    def by_symbol(self) -> dict[str, ChainQuote]:
        # Preserve the public method's fresh mutable-dict behavior. The
        # simulator uses symbol_index() when it only needs read access.
        return dict(self._symbol_index)


class OptionDataProvider(Protocol):
    """Implementations return the same immutable chain view to every strategy."""

    def get_chain(
        self, underlying: str, *, as_of: datetime, min_dte: int, max_dte: int,
        exercise_style: str = "american",
    ) -> OptionChain: ...

    def underlying_closes(self, underlying: str, *, as_of: datetime, sessions: int) -> list[tuple[date, float]]: ...


def realized_volatility(closes: Sequence[tuple[date, float]], *, sessions_per_year: int = 252) -> float:
    """Annualized close-to-close log-return volatility, as decimal IV."""
    values = [float(close) for _, close in closes if close and math.isfinite(float(close)) and float(close) > 0]
    returns = [math.log(values[index] / values[index - 1]) for index in range(1, len(values))]
    if len(returns) < 2:
        raise ValueError("at least three valid closes are needed for realized volatility")
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    return math.sqrt(max(0.0, variance) * sessions_per_year)


def _parse_contract(row: dict) -> OptionContract:
    return OptionContract(
        symbol=str(row.get("symbol") or "").upper(),
        underlying=str(row.get("underlying_symbol") or row.get("underlying") or "").upper(),
        expiration=_date_from_text(row.get("expiration_date")),
        right=str(row.get("type") or "").lower(),
        strike=float(row.get("strike_price")),
        multiplier=int(float(row.get("size") or row.get("multiplier") or 100)),
        exercise_style=str(row.get("style") or "american").lower(),
        tradable=str(row.get("status") or "active").lower() == "active" and bool(row.get("tradable", True)),
        open_interest=int(row["open_interest"]) if row.get("open_interest") is not None else None,
    )


def _parse_chain_snapshot(
    underlying: str,
    spot: float,
    as_of: datetime,
    contracts: Sequence[dict],
    snapshots: dict | None,
    *,
    feed: str,
    rate: float = 0.04,
    dividend_yield: float = 0.015,
    max_quote_age_seconds: int = 120,
) -> OptionChain:
    output: list[ChainQuote] = []
    quality: set[str] = set()
    for row in contracts:
        try:
            contract = _parse_contract(row)
            raw = row if snapshots is None else snapshots.get(contract.symbol) or {}
            item = parse_snapshot_quote(
                contract, raw, spot=spot, as_of=as_of, feed=feed,
                rate=rate, dividend_yield=dividend_yield,
                max_quote_age_seconds=max_quote_age_seconds,
                inline_historical=snapshots is None,
            )
            if item is None:
                quality.add("stale_or_invalid_option_quotes_excluded")
                continue
            output.append(item)
        except (KeyError, TypeError, ValueError, QuoteParseError, OverflowError):
            quality.add("invalid_contract_or_snapshot_excluded")
            continue
    return OptionChain(underlying.upper(), float(spot), as_of, tuple(output), feed, tuple(sorted(quality)))


def market_pair(
    underlying: str, as_of: datetime, *,
    market_inputs: MarketInputs | None, rate: float, dividend_yield: float,
) -> tuple[float, float]:
    """Per-underlying (rate, dividend yield); scalar arguments are the fallback when no inputs are supplied."""
    if market_inputs is None:
        return rate, dividend_yield
    day = as_of.date()
    return market_inputs.rate(day).value, market_inputs.dividend_yield(underlying, day).value


# Vendor Greeks are used when complete. Verified on 2026-10-27 against six SPY
# option snapshots (tests/fixtures/options/alpaca_spy_snapshot_2026-10-27.json):
# vendor vega / Black-Scholes vega per unit vol = 0.0100 and vendor rho /
# Black-Scholes rho per unit rate = 0.0100, so vendor vega is per vol point and
# vendor rho is per rate point. Vendor theta / Black-Scholes theta per calendar
# day = 0.996-1.023, so theta is per day. The model uses per-unit vol and rate,
# so vega and rho are multiplied by 100. Gamma and delta were not rescaled.
VENDOR_VEGA_PER_VOL_POINT_SCALE = 100.0
VENDOR_RHO_PER_RATE_POINT_SCALE = 100.0
VENDOR_THETA_PER_DAY_SCALE = 1.0
_VENDOR_GREEK_SCALE = {
    "delta": 1.0,
    "gamma": 1.0,
    "theta": VENDOR_THETA_PER_DAY_SCALE,
    "vega": VENDOR_VEGA_PER_VOL_POINT_SCALE,
    "rho": VENDOR_RHO_PER_RATE_POINT_SCALE,
}


def _vendor_greeks(raw: object) -> dict[str, float] | None:
    """Vendor Greeks in model units, or None unless every field is present and plausible."""
    if not isinstance(raw, dict):
        return None
    output: dict[str, float] = {}
    for name, scale in _VENDOR_GREEK_SCALE.items():
        try:
            value = float(raw.get(name))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value):
            return None
        output[name] = value * scale
    if not -1.0 <= output["delta"] <= 1.0 or output["gamma"] < 0 or output["vega"] < 0:
        return None
    return output


@lru_cache(maxsize=16384)
def _date_from_text(value: object) -> date:
    """Expiration dates repeat across a chain; one shared object per distinct value."""
    return date.fromisoformat(str(value))


def parse_snapshot_quote(
    contract: OptionContract,
    raw: dict,
    *,
    spot: float,
    as_of: datetime,
    feed: str,
    rate: float = 0.04,
    dividend_yield: float = 0.015,
    max_quote_age_seconds: int = 120,
    american_model: bool = False,
    inline_historical: bool = False,
) -> ChainQuote | None:
    if inline_historical:
        quote = {
            "bp": raw.get("bid"),
            "ap": raw.get("ask"),
            "t": raw.get("quote_at", as_of),
        }
    else:
        quote = raw.get("latestQuote") or raw.get("latest_quote") or raw.get("quote") or {}
    parsed = parse_quote(quote)
    age = (as_of.astimezone(timezone.utc) - parsed["timestamp"]).total_seconds()
    if age < 0 or age > max_quote_age_seconds or parsed["price"] <= 0:
        return None
    years = max((contract.expiration - as_of.date()).days / 365.0, 1.0 / 365.0)
    raw_greeks = raw.get("greeks") or {}
    raw_iv = (
        raw.get("iv") if inline_historical
        else raw.get("impliedVolatility", raw.get("implied_volatility"))
    )
    if raw_iv is None:
        raw_iv = raw_greeks.get("iv", raw_greeks.get("impliedVolatility", raw_greeks.get("implied_volatility")))
    try:
        iv = float(raw_iv)
        if iv > 5.0:
            iv /= 100.0
        if not math.isfinite(iv) or iv <= 0:
            raise ValueError
    except (TypeError, ValueError):
        iv = implied_volatility(
            parsed["price"], spot, contract.strike, years, rate,
            contract.right, dividend_yield=dividend_yield,
            american=american_model and contract.exercise_style == "american",
        )
    vendor = _vendor_greeks(raw_greeks)
    if vendor is not None:
        greeks = GreeksSnapshot(
            delta=vendor["delta"], gamma=vendor["gamma"], theta=vendor["theta"],
            vega=vendor["vega"], rho=vendor["rho"], implied_volatility=iv,
            observed_at=parsed["timestamp"], source="vendor_snapshot_greeks",
        )
    else:
        model = model_greeks(
            spot, contract.strike, years, rate, iv, contract.right,
            dividend_yield=dividend_yield,
            american=american_model and contract.exercise_style == "american",
        )
        greeks = GreeksSnapshot(
            delta=model.delta, gamma=model.gamma, theta=model.theta,
            vega=model.vega, rho=model.rho, implied_volatility=iv,
            observed_at=parsed["timestamp"], source="crr_iv_surface",
        )
    return ChainQuote(contract, parsed["bid"], parsed["ask"], greeks, parsed["timestamp"], feed)


class AlpacaOptionDataProvider:
    """Alpaca market-data adapter. Trading credentials remain in the paper client."""

    def __init__(
        self,
        client,
        *,
        feed: str = "opra",
        max_quote_age_seconds: int = 120,
        rate: float = 0.04,
        dividend_yield: float = 0.015,
        market_inputs: MarketInputs | None = None,
    ) -> None:
        if feed not in {"opra", "indicative"}:
            raise ValueError("options feed must be opra or indicative")
        self.client = client
        self.feed = feed
        self.max_quote_age_seconds = int(max_quote_age_seconds)
        self.rate = float(rate)
        self.dividend_yield = float(dividend_yield)
        self.market_inputs = market_inputs

    def get_chain(
        self,
        underlying: str,
        *,
        as_of: datetime,
        min_dte: int,
        max_dte: int,
        exercise_style: str = "american",
    ) -> OptionChain:
        if as_of.tzinfo is None or min_dte < 0 or max_dte < min_dte:
            raise ValueError("invalid chain as_of or DTE range")
        if exercise_style not in {"american", "european"}:
            raise ValueError("exercise_style must be american or european")
        spot = self.client.underlying_price(underlying)
        contracts = self.client.list_contracts(
            underlying,
            expiration_date_gte=(as_of.date() + timedelta(days=min_dte)).isoformat(),
            expiration_date_lte=(as_of.date() + timedelta(days=max_dte)).isoformat(),
            style=exercise_style,
        )
        contracts = [
            row for row in contracts
            if spot * 0.75 <= float(row.get("strike_price") or 0) <= spot * 1.25
        ]
        if contracts:
            target_dte = (min_dte + max_dte) // 2
            closest_expiration = min(
                {str(row.get("expiration_date")) for row in contracts},
                key=lambda value: abs((date.fromisoformat(value) - as_of.date()).days - target_dte),
            )
            contracts = [row for row in contracts if str(row.get("expiration_date")) == closest_expiration]
        snapshot_rows: dict = {}
        symbols = [str(row.get("symbol") or "") for row in contracts if row.get("symbol")]
        for offset in range(0, len(symbols), 100):
            snapshot_rows.update(self.client.option_snapshots(symbols[offset:offset + 100], feed=self.feed))
        # Live snapshots are fetched after the cycle's initial as_of. Anchor
        # quote freshness to the time the snapshots were received, while
        # historical replay keeps its strict snapshot timestamp checks.
        quote_as_of = max(as_of.astimezone(timezone.utc), datetime.now(timezone.utc))
        rate, dividend_yield = market_pair(
            underlying, as_of, market_inputs=self.market_inputs,
            rate=self.rate, dividend_yield=self.dividend_yield,
        )
        return _parse_chain_snapshot(
            underlying, spot, quote_as_of, contracts, snapshot_rows, feed=self.feed,
            rate=rate, dividend_yield=dividend_yield,
            max_quote_age_seconds=self.max_quote_age_seconds,
        )

    def underlying_closes(self, underlying: str, *, as_of: datetime, sessions: int) -> list[tuple[date, float]]:
        end = as_of.date() - timedelta(days=1)
        start = end - timedelta(days=max(sessions * 2, 90))
        bars = self.client.stock_bars(underlying, start=start, end=end, limit=max(sessions * 2, 100))
        result = []
        for row in bars:
            try:
                day = date.fromisoformat(str(row.get("t"))[:10])
                close = float(row["c"])
                if close > 0 and math.isfinite(close):
                    result.append((day, close))
            except (TypeError, ValueError, KeyError):
                continue
        return sorted(result)[-sessions:]


class HistoricalChainSnapshotProvider:
    """Replay normalized historical chain rows without contacting a broker.

    Each row uses ``underlying``, ``spot``, ``as_of`` and a ``contracts`` list;
    each contract row includes contract fields plus bid/ask, quote timestamp,
    and optional ``iv``. This form can be exported directly from a data vendor.
    """

    def __init__(
        self,
        snapshots: Iterable[dict],
        underlying_history: dict[str, Sequence[tuple[date, float]]] | None = None,
        *,
        rate: float = 0.04,
        dividend_yield: float = 0.015,
        market_inputs: MarketInputs | None = None,
    ) -> None:
        self.snapshots = tuple(snapshots)
        self.market_inputs = market_inputs
        indexed: dict[str, list[tuple[datetime, dict]]] = {}
        for row in self.snapshots:
            symbol = str(row.get("underlying", "")).upper()
            stamp = datetime.fromisoformat(str(row["as_of"]).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                continue
            indexed.setdefault(symbol, []).append((stamp, row))
        self._snapshots_by_underlying = {
            symbol: sorted(rows, key=lambda item: item[0])
            for symbol, rows in indexed.items()
        }
        self._snapshot_times = {
            symbol: [stamp for stamp, _ in rows]
            for symbol, rows in self._snapshots_by_underlying.items()
        }
        self.underlying_history = {key.upper(): tuple(value) for key, value in (underlying_history or {}).items()}
        self.rate = float(rate)
        self.dividend_yield = float(dividend_yield)

    def get_chain(self, underlying: str, *, as_of: datetime, min_dte: int, max_dte: int) -> OptionChain:
        symbol = underlying.upper()
        snapshots = self._snapshots_by_underlying.get(symbol, ())
        times = self._snapshot_times.get(symbol, ())
        index = bisect_right(times, as_of) - 1
        if index < 0:
            raise ValueError(f"no historical option-chain snapshot at or before {as_of.isoformat()}")
        stamp, snapshot = snapshots[index]
        contracts = []
        for row in snapshot.get("contracts", []):
            expiration = date.fromisoformat(str(row.get("expiration_date")))
            dte = (expiration - stamp.date()).days
            if not min_dte <= dte <= max_dte:
                continue
            contracts.append(row)
        rate, dividend_yield = market_pair(
            underlying, stamp, market_inputs=self.market_inputs,
            rate=self.rate, dividend_yield=self.dividend_yield,
        )
        return _parse_chain_snapshot(
            underlying, float(snapshot["spot"]), stamp, contracts, None,
            feed=str(snapshot.get("feed", "historical")),
            rate=rate, dividend_yield=dividend_yield,
            max_quote_age_seconds=10**9,
        )

    def underlying_closes(self, underlying: str, *, as_of: datetime, sessions: int) -> list[tuple[date, float]]:
        return [row for row in self.underlying_history.get(underlying.upper(), ()) if row[0] < as_of.date()][-sessions:]


class ModelSurfaceDataProvider:
    """Generate backtest chains from a supplied point-in-time IV surface.

    ``iv_surface(underlying, expiration, strike, right, as_of)`` must return a
    decimal IV. The caller supplies spot and listed expirations so the model
    never invents historical corporate actions or option listings.
    """

    def __init__(
        self,
        *,
        spot_provider: Callable[[str, datetime], float],
        iv_surface: Callable[[str, date, float, str, datetime], float],
        expirations_provider: Callable[[str, datetime, int, int], Sequence[date]],
        rate: float = 0.04,
        dividend_yield: float = 0.015,
        strike_steps: int = 15,
        strike_step_pct: float = 0.025,
        synthetic_spread_pct: float = 0.04,
        exercise_style: str = "american",
        underlying_closes_provider: Callable[[str, datetime, int], Sequence[tuple[date, float]]] | None = None,
        market_inputs: MarketInputs | None = None,
    ) -> None:
        self.market_inputs = market_inputs
        self.spot_provider = spot_provider
        self.iv_surface = iv_surface
        self.expirations_provider = expirations_provider
        self.rate, self.dividend_yield = float(rate), float(dividend_yield)
        self.strike_steps, self.strike_step_pct = int(strike_steps), float(strike_step_pct)
        self.synthetic_spread_pct = float(synthetic_spread_pct)
        if exercise_style not in {"american", "european"}:
            raise ValueError("model exercise_style must be american or european")
        self.exercise_style = exercise_style
        self.underlying_closes_provider = underlying_closes_provider

    def get_chain(self, underlying: str, *, as_of: datetime, min_dte: int, max_dte: int) -> OptionChain:
        spot = float(self.spot_provider(underlying.upper(), as_of))
        if spot <= 0 or not math.isfinite(spot):
            raise ValueError("model provider received invalid spot")
        rate, dividend_yield = market_pair(
            underlying, as_of, market_inputs=self.market_inputs,
            rate=self.rate, dividend_yield=self.dividend_yield,
        )
        rows = []
        for expiration in self.expirations_provider(underlying.upper(), as_of, min_dte, max_dte):
            dte = (expiration - as_of.date()).days
            if not min_dte <= dte <= max_dte:
                continue
            years = max(dte / 365.0, 1.0 / 365.0)
            step = max(0.5, spot * self.strike_step_pct)
            center = round(spot / step) * step
            for offset in range(-self.strike_steps, self.strike_steps + 1):
                strike = round(center + offset * step, 2)
                if strike <= 0:
                    continue
                for right in ("put", "call"):
                    iv = float(self.iv_surface(underlying.upper(), expiration, strike, right, as_of))
                    greeks = model_greeks(
                        spot, strike, years, rate, iv, right,
                        dividend_yield=dividend_yield,
                        american=self.exercise_style == "american",
                    )
                    half = max(0.01, greeks.price * self.synthetic_spread_pct / 2.0)
                    rows.append(ChainQuote(
                        OptionContract(
                            symbol=f"MODEL:{underlying.upper()}:{expiration:%Y%m%d}:{right[0].upper()}:{strike:g}",
                            underlying=underlying.upper(), expiration=expiration, right=right,
                            strike=strike, exercise_style=self.exercise_style, tradable=False,
                        ),
                        max(0.01, greeks.price - half), greeks.price + half,
                        GreeksSnapshot(
                            delta=greeks.delta, gamma=greeks.gamma, theta=greeks.theta,
                            vega=greeks.vega, rho=greeks.rho, implied_volatility=iv,
                            observed_at=as_of, source=f"{self.exercise_style}_iv_surface_model",
                        ), as_of, "model",
                    ))
        return OptionChain(underlying.upper(), spot, as_of, tuple(rows), "model", ("synthetic_quotes",))

    def underlying_closes(self, underlying: str, *, as_of: datetime, sessions: int) -> list[tuple[date, float]]:
        if self.underlying_closes_provider is None:
            raise NotImplementedError("supply underlying_closes_provider for model-chain strategy replay")
        return list(self.underlying_closes_provider(underlying.upper(), as_of, sessions))


__all__ = [
    "AlpacaOptionDataProvider", "ChainQuote", "HistoricalChainSnapshotProvider",
    "ModelSurfaceDataProvider", "OptionChain", "OptionDataProvider", "parse_snapshot_quote", "realized_volatility",
]

ChainQuote.__reduce__ = _compact_reduce
