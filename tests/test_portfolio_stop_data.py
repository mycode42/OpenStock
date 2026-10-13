"""Read-only market data and fully mocked execution; never contact a broker."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace
import pytest
import requests
import portfolio_monitor as pm

ET = pm.MARKET_TZ
NOW = datetime(2026, 10, 13, 9, 33, 20, tzinfo=ET)
OPEN = NOW.replace(hour=9, minute=30, second=0)
CLOSE = NOW.replace(hour=16, minute=0, second=0)

class Clock(datetime):
    value = NOW
    @classmethod
    def now(cls, tz=None):
        return cls.value.astimezone(tz) if tz else cls.value.replace(tzinfo=None)

class Response:
    def __init__(self, payload=None, status=200, headers=None):
        self.payload, self.status_code, self.headers = payload, status, headers or {}
    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

def bar(timestamp, **updates):
    return {"t": timestamp.isoformat(), "o": 105, "h": 110, "l": 100, "c": 106,
            "v": 1000, "n": 20, "vw": 105, **updates}

def payload(bars=None, token=None, symbol="AAPL", **updates):
    return {"symbol": symbol, "bars": bars if bars is not None else
            [bar(OPEN + timedelta(minutes=i)) for i in range(3)],
            "next_page_token": token, **updates}

@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    Clock.value = NOW
    monkeypatch.setattr(pm, "datetime", Clock)
    monkeypatch.setattr(pm, "_active_mode", "swing")
    monkeypatch.setattr(pm, "_headers", lambda: {"mock": "only"})
    monkeypatch.setattr(pm, "regular_feed", lambda: "sip")
    monkeypatch.setattr(pm, "get_day_schedule", lambda **kw: SimpleNamespace(
        trading_date=NOW.date(), opens_at=OPEN, closes_at=CLOSE))
    monkeypatch.setattr(requests.sessions.Session, "request", lambda *a, **k: pytest.fail("network forbidden"))
    monkeypatch.setattr(pm.yf, "Ticker", lambda *a, **k: pytest.fail("Yahoo forbidden"))
    monkeypatch.setattr(pm.time, "sleep", lambda delay: None)

def mock_get(monkeypatch, responses):
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        value = responses[len(calls)-1]
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr(pm.requests, "get", get)
    return calls

def test_complete_paginated_extrema_cached_once(monkeypatch):
    bars = payload()["bars"]
    calls = mock_get(monkeypatch, [Response(payload(bars[:1], "page2")), Response(payload(bars[1:]))])
    cache = pm.SessionExtrema()
    data = cache.get("aapl")
    assert data["status"] == "ok" and data["high"] == 110 and data["low"] == 100
    assert data["as_of"] == (OPEN + timedelta(minutes=2)).isoformat()
    assert cache.get("AAPL") is data and len(calls) == 2
    assert calls[0][1]["params"]["start"] == OPEN.isoformat()
    assert calls[0][1]["params"]["feed"] == "sip"
    assert calls[0][1]["params"]["timeframe"] == "1Min"
    assert datetime.fromisoformat(calls[0][1]["params"]["end"]) < NOW.replace(second=0)
    assert calls[1][1]["params"]["page_token"] == "page2"
    assert calls[0][1]["timeout"] == (3, 5)

@pytest.mark.parametrize("body,code", [
    (None, "malformed_response"), ({}, "malformed_response"),
    (payload([], symbol="MSFT"), "malformed_response"),
    (payload([], currency="EUR"), "malformed_response"),
    ({"symbol":"AAPL", "bars":None,"next_page_token":None}, "malformed_response"),
    (payload([]), "no_bars"),
    (payload([bar(OPEN)]), "incomplete_session_coverage"),
    (payload([bar(OPEN),bar(OPEN+timedelta(minutes=2))]), "incomplete_session_coverage"),
    (payload([bar(OPEN, h=None)]), "malformed_bar"),
    (payload([bar(OPEN, v=None)]), "malformed_bar"),
    (payload([bar(OPEN, n=0)]), "malformed_bar"),
    (payload([bar(OPEN, vw=float('nan'))]), "malformed_bar"),
    (payload([bar(OPEN, h=float('nan'))]), "malformed_bar"),
    (payload([bar(OPEN, l=-1)]), "malformed_bar"),
    (payload([bar(OPEN, h=float('inf'))]), "malformed_bar"),
    (payload([bar(OPEN, h=True)]), "malformed_bar"),
    (payload([bar(OPEN, h=99)]), "malformed_bar"),
    (payload([None]), "malformed_bar"),
    (payload([bar(OPEN, t=OPEN.replace(tzinfo=None).isoformat())]), "malformed_bar"),
    (ValueError("secret body"), "malformed_json"),
])
def test_unavailable_data_has_no_extrema(monkeypatch, body, code):
    calls = mock_get(monkeypatch, [Response(body)])
    data = pm.SessionExtrema().get("AAPL")
    assert data["status"] != "ok" and data["code"] == code
    assert data["high"] is None and data["low"] is None and len(calls) == 1
    assert "secret" not in str(data)

@pytest.mark.parametrize("timestamp", [OPEN-timedelta(days=1), OPEN-timedelta(minutes=1),
    CLOSE, NOW.replace(second=0), NOW+timedelta(minutes=1), OPEN+timedelta(seconds=1)])
def test_wrong_day_extended_future_partial_bars_rejected(monkeypatch, timestamp):
    mock_get(monkeypatch, [Response(payload([bar(timestamp)]))])
    data = pm.SessionExtrema().get("AAPL")
    assert data["code"] == "bar_outside_completed_session" and data["high"] is None

@pytest.mark.parametrize("bars", [[bar(OPEN),bar(OPEN)], [bar(OPEN+timedelta(minutes=1)),bar(OPEN)]])
def test_duplicate_unsorted_bars_rejected(monkeypatch, bars):
    mock_get(monkeypatch, [Response(payload(bars))])
    assert pm.SessionExtrema().get("AAPL")["code"] == "duplicate_or_unsorted_bar"

def test_pagination_cycle_and_page_bound(monkeypatch):
    calls = mock_get(monkeypatch, [Response(payload([], "repeat"))]*2)
    assert pm.SessionExtrema().get("AAPL")["code"] == "invalid_pagination" and len(calls) == 2
    calls = mock_get(monkeypatch, [Response(payload([], str(i))) for i in range(10)])
    assert pm.SessionExtrema().get("AAPL")["code"] == "pagination_limit_exceeded" and len(calls) == 10

@pytest.mark.parametrize("time,code", [(OPEN,"no_completed_bar_yet"),
    (OPEN+timedelta(seconds=59),"no_completed_bar_yet"),
    (OPEN-timedelta(minutes=1),"outside_regular_session"), (CLOSE,"outside_regular_session")])
def test_opening_and_closed_semantics_make_no_bar_request(monkeypatch, time, code):
    Clock.value = time
    data = pm.SessionExtrema().get("AAPL")
    assert data["status"] == "degraded" and data["code"] == code

def test_first_closed_minute_and_early_close(monkeypatch):
    Clock.value = OPEN + timedelta(minutes=1,seconds=5)
    mock_get(monkeypatch, [Response(payload([bar(OPEN)]))])
    assert pm.SessionExtrema().get("AAPL")["status"] == "ok"
    Clock.value = NOW.replace(hour=13)
    monkeypatch.setattr(pm, "get_day_schedule", lambda **kw: SimpleNamespace(
        trading_date=NOW.date(),opens_at=OPEN,closes_at=NOW.replace(hour=13,minute=0,second=0)))
    assert pm.SessionExtrema().get("AAPL")["code"] == "outside_regular_session"

def test_calendar_missing_failed_wrong_date(monkeypatch):
    monkeypatch.setattr(pm,"get_day_schedule",lambda **kw: None)
    assert pm.SessionExtrema().get("AAPL")["code"] == "no_regular_session"
    def failed(**kw): raise RuntimeError("sensitive")
    monkeypatch.setattr(pm,"get_day_schedule",failed)
    cache = pm.SessionExtrema()
    assert cache.get("AAPL")["code"] == "session_calendar_unavailable"
    assert cache.get("MSFT")["code"] == "session_calendar_unavailable"
    monkeypatch.setattr(pm,"get_day_schedule",lambda **kw: SimpleNamespace(
        trading_date=(NOW-timedelta(days=1)).date(),opens_at=OPEN,closes_at=CLOSE))
    assert pm.SessionExtrema().get("AAPL")["code"] == "invalid_session_calendar"

@pytest.mark.parametrize("status,attempts", [(401,1),(403,1),(400,1),(429,3),(500,3),(502,3),(503,3),(504,3)])
def test_provider_retry_bounds(monkeypatch,status,attempts):
    calls = mock_get(monkeypatch,[Response(status=status)]*attempts)
    data = pm.SessionExtrema().get("AAPL")
    assert data["code"] == f"provider_http_{status}" and len(calls) == attempts
    assert data["high"] is None

def test_recovery_transport_and_rate_limits(monkeypatch):
    waits = []
    monkeypatch.setattr(pm.time,"sleep",waits.append)
    calls = mock_get(monkeypatch,[Response(status=429,headers={"Retry-After":"2"}),Response(payload())])
    assert pm.SessionExtrema().get("AAPL")["status"] == "ok"
    assert len(calls) == 2 and waits == [2]
    waits.clear()
    mock_get(monkeypatch,[Response(status=429,headers={"Retry-After":format_datetime(NOW.astimezone(timezone.utc)+timedelta(seconds=3),usegmt=True)}),Response(payload())])
    assert pm.SessionExtrema().get("AAPL")["status"] == "ok" and waits == [3]
    calls = mock_get(monkeypatch,[requests.Timeout("sensitive")]*3)
    assert pm.SessionExtrema().get("AAPL")["code"] == "provider_transport_failed" and len(calls) == 3
    calls = mock_get(monkeypatch,[Response(status=500),Response(payload())])
    assert pm.SessionExtrema().get("AAPL")["status"] == "ok" and len(calls)==2

@pytest.mark.parametrize("header", ["60","nan","-1","nonsense"])
def test_invalid_or_excess_retry_after_not_retried_early(monkeypatch,header):
    calls = mock_get(monkeypatch,[Response(status=429,headers={"Retry-After":header})])
    assert pm.SessionExtrema().get("AAPL")["status"] == "failed" and len(calls)==1

def test_request_deadline(monkeypatch):
    ticks = iter([0,0,31])
    monkeypatch.setattr(pm.time,"monotonic",lambda: next(ticks))
    calls = mock_get(monkeypatch,[Response(status=503)])
    assert pm.SessionExtrema().get("AAPL")["code"] == "request_budget_exhausted" and len(calls)==1

def setup_monitor(monkeypatch, positions):
    monkeypatch.setattr(pm,"get_account_for_mode",lambda mode: SimpleNamespace(name="Mock"))
    monkeypatch.setattr(pm,"get_mode_config",lambda mode: {"label":"Mock", "risk":{
        "stop_loss_pct":.1, "trailing_stop_pct":.05,"flatten_eod":False}})
    monkeypatch.setattr(pm,"get_account",lambda: {"equity":1000,"cash":100,"last_equity":1000})
    monkeypatch.setattr(pm,"get_positions",lambda: positions)
    monkeypatch.setattr(pm,"get_spy_daily_return",lambda: None)
    closes = []
    def close(symbol,qty,reason,dry_run):
        closes.append((symbol,qty,reason,dry_run))
        return {"status":"dry_run","symbol":symbol,"qty":qty,"reason":reason}
    monkeypatch.setattr(pm,"place_market_close",close)
    return closes

def position(symbol="AAPL",qty=1,price=106,entry=106):
    return {"symbol":symbol,"qty":str(qty),"current_price":str(price),
            "avg_entry_price":str(entry),"market_value":"100","unrealized_plpc":"0"}

@pytest.mark.parametrize("qty,price,should_trigger",[(1,100,True),(-1,106,True),(1,106,False),(-1,102,False),(-.1,106,True)])
def test_long_short_exact_stop_semantics_summary_uses_cache(monkeypatch,capsys,qty,price,should_trigger):
    closes = setup_monitor(monkeypatch,[position(qty=qty,price=price),position(qty=qty,price=price)])
    calls = mock_get(monkeypatch,[Response(payload())])
    result = pm.run_monitor(dry_run=True,mode="swing")
    assert result["success"] and result["status"]=="ok" and pm.monitor_exit_code(result)==0
    assert len(closes) == (2 if should_trigger else 0) and len(calls)==1
    assert all(a["stop_type"]=="trailing_stop" for a in result["actions"])
    assert "$100.00" in capsys.readouterr().out.replace(" ", "") if qty<0 else True

@pytest.mark.parametrize("body",[payload([]),payload([bar(OPEN,h=None)]),payload([bar(OPEN-timedelta(days=1))])])
def test_missing_malformed_stale_no_trailing_orders_valid_hard_checks_continue(monkeypatch,body):
    closes = setup_monitor(monkeypatch,[position(price=100,entry=100),
        position("MSFT",price=80,entry=100),position("TSLA",qty=-1,price=120,entry=100)])
    calls=mock_get(monkeypatch,[Response(body),Response(payload([],symbol="MSFT")),Response(payload([],symbol="TSLA"))])
    result=pm.run_monitor(dry_run=True,mode="swing")
    assert not result["success"] and result["status"]=="degraded" and pm.monitor_exit_code(result)==2
    assert {c[0] for c in closes}=={"MSFT","TSLA"}
    assert all(a["stop_type"]=="hard_stop" for a in result["actions"])
    assert len(result["stop_data"])==3 and len(calls)==3

def test_one_failed_symbol_does_not_block_valid_symbol(monkeypatch):
    closes=setup_monitor(monkeypatch,[position(),position("MSFT",price=100)])
    mock_get(monkeypatch,[Response(status=403),Response(payload(symbol="MSFT"))])
    result=pm.run_monitor(dry_run=True,mode="swing")
    assert not result["success"] and [c[0] for c in closes]==["MSFT"]

@pytest.mark.parametrize("price",[float('nan'),float('inf'),0])
def test_invalid_position_price_cannot_submit(monkeypatch,price):
    closes=setup_monitor(monkeypatch,[position(price=price)])
    mock_get(monkeypatch,[Response(payload())])
    result=pm.run_monitor(dry_run=True,mode="swing")
    assert not result["success"] and not closes
    assert result["stop_data"]["AAPL"]["code"]=="invalid_position_price_or_quantity"

def test_snapshot_expired_during_fetch(monkeypatch):
    def get(*a,**kw):
        Clock.value=NOW+timedelta(seconds=61)
        return Response(payload())
    monkeypatch.setattr(pm.requests,"get",get)
    data=pm.SessionExtrema().get("AAPL")
    assert data["code"]=="snapshot_expired" and data["high"] is None


def test_cached_extrema_expire_without_refetch(monkeypatch):
    calls=mock_get(monkeypatch,[Response(payload())])
    cache=pm.SessionExtrema()
    assert cache.get("AAPL")["status"]=="ok"
    Clock.value=NOW+timedelta(seconds=61)
    assert cache.get("AAPL")["code"]=="snapshot_expired" and len(calls)==1
    assert cache.get("AAPL")["high"] is None


def test_rate_limit_reset_wait_bound(monkeypatch):
    reset=str(NOW.astimezone(timezone.utc).timestamp()+60)
    calls=mock_get(monkeypatch,[Response(status=429,headers={"X-RateLimit-Reset":reset})])
    assert pm.SessionExtrema().get("AAPL")["code"]=="retry_after_exceeds_budget"
    assert len(calls)==1


def test_slow_success_after_request_budget_is_not_protection_data(monkeypatch):
    ticks=iter([0,0,31])
    monkeypatch.setattr(pm.time,"monotonic",lambda: next(ticks))
    calls=mock_get(monkeypatch,[Response(payload())])
    result=pm.SessionExtrema().get("AAPL")
    assert result["code"]=="request_budget_exhausted" and result["high"] is None
    assert len(calls)==1
