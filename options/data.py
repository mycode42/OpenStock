"""Option-chain provider interfaces for live paper data and backtest replay."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import math
from typing import Callable, Iterable, Protocol, Sequence

from market.quotes import QuoteParseError, parse_quote
from options.models import GreeksSnapshot, OptionContract, OptionLeg
from options.pricing import implied_volatility, model_greeks, years_to_expiration


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

    def expirations(self, *, min_dte: int = 0, max_dte: int = 365) -> tuple[date, ...]:
        today = self.as_of.date()
        return tuple(sorted({
            row.contract.expiration for row in self.contracts
            if min_dte <= (row.contract.expiration - today).days <= max_dte
        }))

    def for_expiration(self, expiration: date) -> tuple[ChainQuote, ...]:
        return tuple(row for row in self.contracts if row.contract.expiration == expiration)

    def by_symbol(self) -> dict[str, ChainQuote]:
        return {row.contract.symbol: row for row in self.contracts}


class OptionDataProvider(Protocol):
    """Implementations return the same immutable chain view to every strategy."""

    def get_chain(
        self, underlying: str, *, as_of: datetime, min_dte: int, max_dte: int,
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
        expiration=date.fromisoformat(str(row.get("expiration_date"))),
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
    snapshots: dict,
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
            raw = snapshots.get(contract.symbol) or {}
            item = parse_snapshot_quote(
                contract, raw, spot=spot, as_of=as_of, feed=feed,
                rate=rate, dividend_yield=dividend_yield,
                max_quote_age_seconds=max_quote_age_seconds,
            )
            if item is None:
                quality.add("stale_or_invalid_option_quotes_excluded")
                continue
            output.append(item)
        except (KeyError, TypeError, ValueError, QuoteParseError, OverflowError):
            quality.add("invalid_contract_or_snapshot_excluded")
            continue
    return OptionChain(underlying.upper(), float(spot), as_of, tuple(output), feed, tuple(sorted(quality)))


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
) -> ChainQuote | None:
    quote = raw.get("latestQuote") or raw.get("latest_quote") or raw.get("quote") or {}
    parsed = parse_quote(quote)
    age = (as_of.astimezone(timezone.utc) - parsed["timestamp"]).total_seconds()
    if age < 0 or age > max_quote_age_seconds or parsed["price"] <= 0:
        return None
    years = max((contract.expiration - as_of.date()).days / 365.0, 1.0 / 365.0)
    raw_greeks = raw.get("greeks") or {}
    raw_iv = raw.get("impliedVolatility", raw.get("implied_volatility"))
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
    ) -> None:
        if feed not in {"opra", "indicative"}:
            raise ValueError("options feed must be opra or indicative")
        self.client = client
        self.feed = feed
        self.max_quote_age_seconds = int(max_quote_age_seconds)
        self.rate = float(rate)
        self.dividend_yield = float(dividend_yield)

    def get_chain(self, underlying: str, *, as_of: datetime, min_dte: int, max_dte: int) -> OptionChain:
        if as_of.tzinfo is None or min_dte < 0 or max_dte < min_dte:
            raise ValueError("invalid chain as_of or DTE range")
        spot = self.client.underlying_price(underlying)
        contracts = self.client.list_contracts(
            underlying,
            expiration_date_gte=(as_of.date() + timedelta(days=min_dte)).isoformat(),
            expiration_date_lte=(as_of.date() + timedelta(days=max_dte)).isoformat(),
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
        return _parse_chain_snapshot(
            underlying, spot, as_of, contracts, snapshot_rows, feed=self.feed,
            rate=self.rate, dividend_yield=self.dividend_yield,
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
    ) -> None:
        self.snapshots = tuple(snapshots)
        self.underlying_history = {key.upper(): tuple(value) for key, value in (underlying_history or {}).items()}
        self.rate = float(rate)
        self.dividend_yield = float(dividend_yield)

    def get_chain(self, underlying: str, *, as_of: datetime, min_dte: int, max_dte: int) -> OptionChain:
        candidates = []
        for row in self.snapshots:
            if str(row.get("underlying", "")).upper() != underlying.upper():
                continue
            stamp = datetime.fromisoformat(str(row["as_of"]).replace("Z", "+00:00"))
            if stamp.tzinfo is None or stamp > as_of:
                continue
            candidates.append((stamp, row))
        if not candidates:
            raise ValueError(f"no historical option-chain snapshot at or before {as_of.isoformat()}")
        stamp, snapshot = max(candidates, key=lambda item: item[0])
        contracts = []
        quote_map = {}
        for row in snapshot.get("contracts", []):
            contract = dict(row)
            expiration = date.fromisoformat(str(contract.get("expiration_date")))
            dte = (expiration - stamp.date()).days
            if not min_dte <= dte <= max_dte:
                continue
            contracts.append(contract)
            quote_map[str(contract["symbol"]).upper()] = {
                "latestQuote": {
                    "bp": contract.get("bid"), "ap": contract.get("ask"),
                    "t": contract.get("quote_at", snapshot["as_of"]),
                },
                "impliedVolatility": contract.get("iv"),
                "greeks": contract.get("greeks", {}),
            }
        return _parse_chain_snapshot(
            underlying, float(snapshot["spot"]), stamp, contracts, quote_map,
            feed=str(snapshot.get("feed", "historical")),
            rate=self.rate, dividend_yield=self.dividend_yield,
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
    ) -> None:
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
                        spot, strike, years, self.rate, iv, right,
                        dividend_yield=self.dividend_yield,
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
