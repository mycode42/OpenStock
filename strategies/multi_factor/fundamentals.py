"""SEC company-facts feed normalized to conservative point-in-time rows."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
from threading import RLock


ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
NORMALIZED_CACHE_TTL_SECONDS = 24 * 60 * 60
NORMALIZED_CACHE_NAMESPACE = "algo_factor_fundamentals_v2"
_CACHE_LOCK = RLock()
_MEMORY_CACHE: dict[str, tuple[float, list[dict], dict]] = {}


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _fact_values(facts: dict, concepts: tuple[str, ...], *, units: tuple[str, ...], accession: str, end: str, annual_duration: bool) -> float | None:
    taxonomy = (facts.get("facts") or {}).get("us-gaap") or {}
    for concept in concepts:
        concept_data = taxonomy.get(concept) or {}
        unit_data = concept_data.get("units") or {}
        for unit in units:
            for entry in unit_data.get(unit, []) or []:
                if str(entry.get("accn", "")) != accession or str(entry.get("end", "")) != end:
                    continue
                start = entry.get("start")
                if annual_duration:
                    if not start:
                        continue
                    try:
                        days = (date.fromisoformat(end) - date.fromisoformat(str(start))).days + 1
                    except ValueError:
                        continue
                    if not 300 <= days <= 400:
                        continue
                elif start:
                    continue
                value = _finite(entry.get("val"))
                if value is not None:
                    return value
    return None


def _annual_rows(facts: dict) -> list[dict]:
    """Extract annual filing events; ``filed`` is the source of availability."""
    taxonomy = (facts.get("facts") or {}).get("us-gaap") or {}
    events: dict[tuple[str, str, str], dict] = {}
    for concept_data in taxonomy.values():
        for entries in (concept_data.get("units") or {}).values():
            for entry in entries or []:
                form = str(entry.get("form", "")).upper()
                accession = str(entry.get("accn", ""))
                filed = str(entry.get("filed", ""))
                end = str(entry.get("end", ""))
                if form not in ANNUAL_FORMS or not accession or not filed or not end:
                    continue
                try:
                    filed_date = date.fromisoformat(filed)
                    end_date = date.fromisoformat(end)
                except ValueError:
                    continue
                if entry.get("start"):
                    try:
                        duration = (end_date - date.fromisoformat(str(entry["start"]))).days + 1
                    except ValueError:
                        continue
                    if not 300 <= duration <= 400:
                        continue
                events[(accession, filed, end)] = {
                    "accession": accession, "filed_at": filed,
                    "period_end": end, "form": form,
                    "available_at": (filed_date + timedelta(days=1)).isoformat() + "T00:00:00Z",
                }

    output = []
    for event in sorted(events.values(), key=lambda row: (row["available_at"], row["period_end"], row["accession"])):
        accession, end = event["accession"], event["period_end"]
        def flow(concepts: tuple[str, ...], units: tuple[str, ...] = ("USD",)) -> float | None:
            return _fact_values(
                facts, concepts, units=units, accession=accession, end=end,
                annual_duration=True,
            )

        def instant(concepts: tuple[str, ...], units: tuple[str, ...] = ("USD",)) -> float | None:
            return _fact_values(
                facts, concepts, units=units, accession=accession, end=end,
                annual_duration=False,
            )

        net_income = flow(("NetIncomeLoss", "ProfitLoss"))
        eps = flow(
            ("EarningsPerShareDiluted", "EarningsPerShareBasic"),
            ("USD/shares", "USD / shares"),
        )
        weighted_shares = flow(
            ("WeightedAverageNumberOfDilutedSharesOutstanding", "WeightedAverageNumberOfSharesOutstandingBasic"),
            ("shares",),
        )
        shares = instant(
            ("EntityCommonStockSharesOutstanding", "CommonStockSharesOutstanding"),
            ("shares",),
        ) or weighted_shares
        if eps is None and net_income is not None and weighted_shares and weighted_shares > 0:
            eps = net_income / weighted_shares

        equity = instant(("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"))
        assets = instant(("Assets",))
        liabilities = instant(("Liabilities",))
        gross_profit = flow(("GrossProfit",))
        if gross_profit is None:
            revenue = flow(("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"))
            cost_of_revenue = flow(("CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsAndServicesSoldExcludingDepreciationDepletionAndAmortization"))
            if revenue is not None and cost_of_revenue is not None:
                gross_profit = revenue - cost_of_revenue

        operating_cash_flow = flow(("NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"))
        capital_expenditure = flow(("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"))
        free_cash_flow_per_share = None
        if operating_cash_flow is not None and capital_expenditure is not None and shares and shares > 0:
            free_cash_flow_per_share = (operating_cash_flow - abs(capital_expenditure)) / shares

        debt_current = instant(("LongTermDebtAndFinanceLeaseObligationsCurrent", "LongTermDebtCurrent", "ShortTermBorrowings"))
        debt_noncurrent = instant(("LongTermDebtAndFinanceLeaseObligationsNoncurrent", "LongTermDebtNoncurrent"))
        debt = None
        if debt_current is not None or debt_noncurrent is not None:
            debt = (debt_current or 0.0) + (debt_noncurrent or 0.0)
        if debt is None:
            debt = instant(("LongTermDebt", "LongTermDebtAndFinanceLeaseObligations"))
        if debt is None:
            debt = liabilities
        debt_to_equity = (
            debt / abs(equity)
            if debt is not None and equity is not None and abs(equity) > 1e-9
            else None
        )

        row = {
            **event,
            "eps_per_share": eps,
            "book_value_per_share": equity / shares if equity is not None and shares and shares > 0 else None,
            "free_cash_flow_per_share": free_cash_flow_per_share,
            "gross_profitability": gross_profit / assets if gross_profit is not None and assets and assets > 0 else None,
            "debt_to_equity": debt_to_equity,
        }
        # Don't retain an accession-only row that has no usable factor values.
        if any(value is not None and math.isfinite(value) for key, value in row.items() if key.endswith("per_share") or key in {"gross_profitability", "debt_to_equity"}):
            output.append(row)
    return output


def load_point_in_time_fundamentals(
    symbols: list[str], *, as_of: date | None = None,
) -> tuple[dict[str, list[dict]], dict]:
    """Load normalized SEC fundamentals with conservative filing availability.

    Raw XBRL is obtained through the repository's SEC companyfacts API/cache.
    Filings are treated as usable on the calendar day after the SEC filing date
    because companyfacts provides a date, not an intraday acceptance timestamp.
    """
    from market import research_data as api_free

    requested = list(dict.fromkeys(str(symbol).upper().strip() for symbol in symbols if str(symbol).strip()))
    coverage_as_of = as_of or datetime.now(timezone.utc).date()
    output: dict[str, list[dict]] = {}
    failures: dict[str, str] = {}
    cache_hits = 0
    for symbol in requested:
        normalized = None
        with _CACHE_LOCK:
            cached = _MEMORY_CACHE.get(symbol)
            if cached and datetime.now(timezone.utc).timestamp() - cached[0] < NORMALIZED_CACHE_TTL_SECONDS:
                normalized = cached[1]
                cache_hits += 1
        if normalized is None:
            normalized = api_free._disk_cache_get(
                NORMALIZED_CACHE_NAMESPACE, symbol, NORMALIZED_CACHE_TTL_SECONDS,
            )
            if isinstance(normalized, list):
                cache_hits += 1
            else:
                try:
                    cik = api_free._resolve_cik(symbol)
                    facts = api_free._get_company_facts_sec(cik) if cik else None
                    normalized = _annual_rows(facts) if facts else []
                    if facts:
                        api_free._disk_cache_set(NORMALIZED_CACHE_NAMESPACE, symbol, normalized)
                except Exception as exc:
                    normalized = []
                    failures[symbol] = type(exc).__name__
            with _CACHE_LOCK:
                _MEMORY_CACHE[symbol] = (datetime.now(timezone.utc).timestamp(), normalized, {})
        if normalized:
            output[symbol] = normalized

    factor_fields = (
        "eps_per_share", "book_value_per_share", "free_cash_flow_per_share",
        "gross_profitability", "debt_to_equity",
    )
    def usable_rows(symbol: str) -> list[dict]:
        rows = []
        for row in output.get(symbol, []):
            try:
                available_date = datetime.fromisoformat(
                    str(row["available_at"]).replace("Z", "+00:00"),
                ).date()
                period_end = date.fromisoformat(str(row["period_end"])[:10])
            except (KeyError, TypeError, ValueError):
                continue
            if available_date > coverage_as_of:
                continue
            if (coverage_as_of - period_end).days > 550:
                continue
            rows.append(row)
        return rows

    coverage = {
        field: sum(any(row.get(field) is not None for row in usable_rows(symbol)) for symbol in requested)
        for field in factor_fields
    }
    covered_symbols = {
        symbol for symbol in requested
        if any(any(row.get(field) is not None for field in factor_fields) for row in usable_rows(symbol))
    }
    visible_output = {
        symbol: [row for row in rows if row.get("available_at", "")[:10] <= coverage_as_of.isoformat()]
        for symbol, rows in output.items()
    }
    normalized_payload = json.dumps(visible_output, sort_keys=True, separators=(",", ":"))
    version = hashlib.sha256(normalized_payload.encode("utf-8")).hexdigest()
    with_facts = len(covered_symbols)
    status = "unavailable" if not with_facts else "complete" if with_facts == len(requested) else "partial"
    return output, {
        "status": status,
        "source": "sec_edgar_companyfacts",
        "coverage_as_of": coverage_as_of.isoformat(),
        "symbols_requested": len(requested),
        "symbols_with_point_in_time_facts": with_facts,
        "symbols_missing_facts": sorted(set(requested) - covered_symbols),
        "factor_symbol_coverage": coverage,
        "filing_record_count": sum(len(rows) for rows in visible_output.values()),
        "cache_hit_count": cache_hits,
        "failure_types": failures,
        "availability_policy": "SEC filed date plus one calendar day; same-day filings excluded conservatively",
        "reporting_period_policy": "annual 10-K/20-F/40-F facts only; factor rows expire 550 days after period end",
        "data_version": version,
    }


def requested_fundamental_factors(
    config, regime: str = "unknown", learned_factor_weights: dict[str, float] | None = None,
    learned_factor_model: dict | None = None,
) -> bool:
    """Whether a factor profile asks for value or quality inputs."""
    if config.factor_weight_mode == "ridge_walk_forward":
        if learned_factor_weights is None:
            return True
        weights = learned_factor_weights
    elif config.factor_weight_mode in {"lightgbm_walk_forward", "xgboost_walk_forward"}:
        if not isinstance(learned_factor_model, dict):
            return True
        weights = {name: 1.0 for name in learned_factor_model.get("factor_names", [])}
    else:
        weights = dict(config.factor_weights)
        weights.update(config.regime_factor_weights.get(str(regime).lower(), {}))
    return any(
        name.startswith(("value_", "quality_")) and float(weight) > 0
        for name, weight in weights.items()
    )


__all__ = ["load_point_in_time_fundamentals", "requested_fundamental_factors"]
