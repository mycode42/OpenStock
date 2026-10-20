"""Point-in-time options research data loading and validation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from options.data import HistoricalChainSnapshotProvider, ModelSurfaceDataProvider, OptionChain, OptionDataProvider
from strategies.event_driven.models import ScheduledEvent


def _timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO-8601 string")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return stamp.astimezone(timezone.utc)


@dataclass(frozen=True)
class ResearchObservation:
    as_of: datetime
    underlying: str
    spot: float
    regime: str
    volatility_state: str
    feed: str
    events: tuple[ScheduledEvent, ...]
    equity_signals: dict[str, object]
    event_recommendations: dict[str, object]
    dividends: dict[str, float]
    source_line: int
    chain: OptionChain | None = None
    chain_error: str | None = None


@dataclass(frozen=True)
class OptionsResearchDataset:
    observations: tuple[ResearchObservation, ...]
    provider: OptionDataProvider
    source_path: Path
    source_sha256: str
    data_mode: str
    underlying_history: dict[str, tuple[tuple[date, float], ...]]


def _load_rows(path: Path) -> tuple[list[dict], dict[str, list[tuple[date, float]]], dict]:
    raw = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
        metadata = {}
    else:
        payload = json.loads(raw)
        if isinstance(payload, list):
            rows, metadata = payload, {}
        elif isinstance(payload, dict):
            rows = payload.get("snapshots", payload.get("observations", []))
            metadata = payload
        else:
            raise ValueError("dataset must be JSON, JSONL, or a JSON array")
    if not isinstance(rows, list) or not rows or any(not isinstance(row, dict) for row in rows):
        raise ValueError("dataset must contain at least one object snapshot")
    history: dict[str, list[tuple[date, float]]] = {}
    for symbol, values in (metadata.get("underlying_history") or {}).items():
        if not isinstance(values, list):
            continue
        history[str(symbol).upper()] = [
            (date.fromisoformat(str(item[0])[:10]), float(item[1]))
            for item in values if isinstance(item, (list, tuple)) and len(item) >= 2
        ]
    return rows, history, metadata


def _normalize_contract(row: dict, *, as_of: datetime, underlying: str) -> dict:
    symbol = str(row.get("symbol") or "").upper()
    if not symbol:
        raise ValueError("each option contract needs its original symbol")
    expiration = row.get("expiration_date", row.get("expiration"))
    right = str(row.get("right", row.get("type", ""))).lower()
    strike = float(row.get("strike", row.get("strike_price")))
    bid, ask = float(row.get("bid", 0)), float(row.get("ask", 0))
    stamp = row.get("quote_at", row.get("observed_at", as_of.isoformat()))
    # The provider consumes Alpaca-shaped rows while preserving the original
    # historical bid, ask, IV, open interest, and exercise style.
    normalized = {
        "symbol": symbol,
        "underlying_symbol": str(row.get("underlying", row.get("underlying_symbol", underlying))).upper(),
        "expiration_date": str(expiration)[:10],
        "type": right,
        "strike_price": strike,
        "size": int(row.get("multiplier", row.get("size", 100))),
        "style": str(row.get("exercise_style", row.get("style", "american"))).lower(),
        "status": "active" if row.get("tradable", True) else "inactive",
        "tradable": bool(row.get("tradable", True)),
        "open_interest": row.get("open_interest"),
        "bid": bid,
        "ask": ask,
        "quote_at": _timestamp(stamp, field="contract.quote_at").isoformat(),
        "iv": row.get("iv", row.get("implied_volatility")),
        "greeks": row.get("greeks", {}),
    }
    if not normalized["underlying_symbol"]:
        raise ValueError(f"contract {symbol} has no underlying")
    if not normalized["expiration_date"] or right not in {"call", "put"}:
        raise ValueError(f"contract {symbol} has invalid expiration or right")
    return normalized


def _signals(raw: Any, *, as_of: datetime) -> dict[str, object]:
    output = {}
    rows = raw.items() if isinstance(raw, dict) else (
        (str(item.get("ticker", item.get("symbol", ""))), item)
        for item in (raw or []) if isinstance(item, dict)
    )
    for ticker, item in rows:
        if not isinstance(item, dict):
            continue
        known = item.get("known_at", item.get("created_at"))
        if known and _timestamp(known, field="signal.known_at") > as_of:
            continue
        # Preserve only point-in-time briefing fields used by the options
        # expression; no LLM or equity model is run during research.
        output[str(ticker).upper()] = SimpleNamespace(
            action=item.get("action", item.get("side", "")),
            confidence=item.get("confidence", item.get("strength", 0.0)),
            strategy=item.get("strategy"),
            run_id=item.get("run_id", item.get("signal_id", item.get("event_id"))),
            reason=item.get("reason", item.get("reasoning", "")),
            generated_at=item.get("generated_at", known),
        )
    return output


def _events(raw: Any, *, as_of: datetime) -> tuple[ScheduledEvent, ...]:
    output = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        try:
            known_at = _timestamp(item["known_at"], field="event.known_at")
            if known_at > as_of:
                continue
            output.append(ScheduledEvent(
                event_id=str(item["event_id"]),
                event_type=str(item["event_type"]),
                ticker=str(item["ticker"]),
                scheduled_at=_timestamp(item["scheduled_at"], field="event.scheduled_at"),
                known_at=known_at,
                reaction_start_at=_timestamp(item["reaction_start_at"], field="event.reaction_start_at"),
                reaction_end_at=_timestamp(item["reaction_end_at"], field="event.reaction_end_at"),
                source=str(item.get("source", "research-dataset")),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(output)


def load_options_research_dataset(
    path: str | Path,
    *,
    underlying: str = "SPY",
    rate: float = 0.04,
    dividend_yield: float = 0.015,
    data_mode: str = "auto",
    model_surface_config: dict | None = None,
) -> OptionsResearchDataset:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"historical options dataset not found: {source}")
    raw_bytes = source.read_bytes()
    rows, history, metadata = _load_rows(source)
    observations: list[ResearchObservation] = []
    normalized_snapshots = []
    target = underlying.upper()
    requested_mode = str(data_mode or "auto").lower()
    if requested_mode not in {"auto", "historical", "historical_chains", "model", "model_surface"}:
        raise ValueError("data_mode must be auto, historical_chains, or model_surface")
    use_model_surface = requested_mode in {"model", "model_surface"} or (
        requested_mode == "auto" and (
            str(metadata.get("data_mode", "")).lower() in {"model", "model_surface"}
            or any(("iv_surface" in row or metadata.get("iv_surface")) and not row.get("contracts") for row in rows)
        )
    )
    model_rows = []
    for line, row in enumerate(rows, start=1):
        symbol = str(row.get("underlying", "")).upper()
        if symbol != target:
            continue
        as_of = _timestamp(row.get("as_of"), field=f"snapshot[{line}].as_of")
        spot = float(row.get("spot", 0))
        contracts = row.get("contracts", [])
        if spot <= 0 or not math.isfinite(spot):
            raise ValueError(f"snapshot line {line} needs a positive finite spot")
        if use_model_surface:
            points = row.get("iv_surface", metadata.get("iv_surface", []))
            if not isinstance(points, (list, dict)) or not points:
                raise ValueError(f"model surface snapshot line {line} needs iv_surface points")
            model_rows.append({"as_of": as_of, "spot": spot, "raw": row})
        else:
            if not isinstance(contracts, list):
                raise ValueError(f"historical snapshot line {line} needs a contracts array")
            normalized_contracts = [
                _normalize_contract(contract, as_of=as_of, underlying=target)
                for contract in contracts
            ]
            normalized_snapshots.append({
                "underlying": target,
                "spot": spot,
                "as_of": as_of.isoformat(),
                "feed": str(row.get("feed", "historical")),
                "contracts": normalized_contracts,
            })
        close = row.get("underlying_close", row.get("close"))
        if close is not None:
            history.setdefault(target, []).append((as_of.date(), float(close)))
        observations.append(ResearchObservation(
            as_of=as_of,
            underlying=target,
            spot=spot,
            regime=str(row.get("regime", "unknown")).lower(),
            volatility_state=str(row.get("volatility_state", "unknown")).lower(),
            feed=("model" if use_model_surface else str(row.get("feed", "historical"))),
            events=_events(row.get("events", []), as_of=as_of),
            equity_signals=_signals(row.get("equity_signals", []), as_of=as_of),
            event_recommendations=_signals(row.get("event_recommendations", []), as_of=as_of),
            dividends={str(k).upper(): float(v) for k, v in (row.get("dividends") or {}).items()},
            source_line=line,
        ))
    if len(observations) < 2:
        raise ValueError(f"dataset has fewer than two {target} research observations")
    observations.sort(key=lambda item: item.as_of)
    if len({item.as_of for item in observations}) != len(observations):
        raise ValueError("duplicate as_of timestamps are not allowed")
    history[target] = sorted(set(history.get(target, [])))
    if not history.get(target):
        # Treating end-of-session spot as a close is only a fallback. The report
        # records this mode because intraday spot cannot safely proxy a close.
        history[target] = [(item.as_of.date(), item.spot) for item in observations]
        close_proxy = True
    else:
        close_proxy = False
    if use_model_surface:
        model_settings = dict(model_surface_config or {})
        indexed_rows = sorted(model_rows, key=lambda item: item["as_of"])

        def row_at(as_of: datetime):
            eligible = [item for item in indexed_rows if item["as_of"] <= as_of]
            if not eligible:
                raise ValueError(f"no point-in-time IV surface at or before {as_of.isoformat()}")
            return eligible[-1]

        def spot_provider(symbol: str, as_of: datetime) -> float:
            return float(row_at(as_of)["spot"])

        def surface_points(item: dict) -> list[dict]:
            points = item["raw"].get("iv_surface", metadata.get("iv_surface", []))
            if isinstance(points, dict):
                points = points.get("points", [])
            return [point for point in points if isinstance(point, dict)] if isinstance(points, list) else []

        def iv_surface(symbol: str, expiration: date, strike: float, right: str, as_of: datetime) -> float:
            item = row_at(as_of)
            spot = float(item["spot"])
            ranked = []
            for point in surface_points(item):
                try:
                    if str(point.get("right", "")).lower() != right:
                        continue
                    point_expiration = date.fromisoformat(str(point.get("expiration", point.get("expiration_date")))[:10])
                    point_strike = float(point["strike"])
                    iv = float(point.get("iv", point.get("implied_volatility")))
                    if iv > 5:
                        iv /= 100.0
                    if iv <= 0 or not math.isfinite(iv) or point_strike <= 0:
                        continue
                    tenor_distance = abs((point_expiration - expiration).days) / 30.0
                    moneyness_distance = abs(math.log(point_strike / spot) - math.log(strike / spot)) / 0.10
                    distance = math.sqrt(tenor_distance * tenor_distance + moneyness_distance * moneyness_distance)
                    ranked.append((distance, iv))
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
            if not ranked:
                raise ValueError(f"IV surface has no usable {right} points at {as_of.isoformat()}")
            ranked.sort(key=lambda value: value[0])
            nearest = ranked[:4]
            if nearest[0][0] <= 1e-12:
                return nearest[0][1]
            weights = [1.0 / max(distance, 0.05) ** 2 for distance, _ in nearest]
            return sum(weight * value for weight, (_, value) in zip(weights, nearest)) / sum(weights)

        def expirations_provider(symbol: str, as_of: datetime, min_dte: int, max_dte: int):
            item = row_at(as_of)
            raw_expirations = item["raw"].get("expirations")
            if not raw_expirations:
                raw_expirations = sorted({
                    str(point.get("expiration", point.get("expiration_date")))[:10]
                    for point in surface_points(item)
                    if point.get("expiration", point.get("expiration_date"))
                })
            return [date.fromisoformat(str(value)[:10]) for value in raw_expirations]

        def closes_provider(symbol: str, as_of: datetime, sessions: int):
            return [value for value in history.get(symbol.upper(), ()) if value[0] < as_of.date()][-sessions:]

        settings = model_settings
        exercise_style = str(settings.get(
            "exercise_style", "european" if target == "SPX" else "american",
        )).lower()
        provider = ModelSurfaceDataProvider(
            spot_provider=spot_provider,
            iv_surface=iv_surface,
            expirations_provider=expirations_provider,
            rate=rate,
            dividend_yield=dividend_yield,
            strike_steps=int(settings.get("strike_steps", 15)),
            strike_step_pct=float(settings.get("strike_step_pct", 0.025)),
            synthetic_spread_pct=float(settings.get("synthetic_spread_pct", 0.04)),
            exercise_style=exercise_style,
            underlying_closes_provider=closes_provider,
        )
        data_mode_name = "model_surface_iv_spot_close_proxy" if close_proxy else "model_surface_iv"
    else:
        provider = HistoricalChainSnapshotProvider(
            normalized_snapshots, history, rate=rate, dividend_yield=dividend_yield,
        )
        data_mode_name = "historical_chains_spot_close_proxy" if close_proxy else "historical_chains"
    prepared_observations = []
    for observation in observations:
        try:
            chain = provider.get_chain(
                target, as_of=observation.as_of, min_dte=0, max_dte=730,
            )
            prepared_observations.append(replace(observation, chain=chain))
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            prepared_observations.append(replace(
                observation, chain_error=f"{type(exc).__name__}: {exc}",
            ))
    return OptionsResearchDataset(
        observations=tuple(prepared_observations),
        provider=provider,
        source_path=source.resolve(),
        source_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        data_mode=("model_or_synthetic_chain_rows" if any(row.feed in {"model", "synthetic"} for row in observations) and not use_model_surface else data_mode_name),
        underlying_history={key: tuple(values) for key, values in history.items()},
    )


__all__ = ["OptionsResearchDataset", "ResearchObservation", "load_options_research_dataset"]
