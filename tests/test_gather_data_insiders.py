import gather_data
from src.data.models import InsiderTrade
from src.tools import api_free


def test_swing_gather_serializes_current_insider_trade_model(monkeypatch):
    trade = InsiderTrade(
        ticker="AAPL",
        issuer="Apple Inc.",
        name="Jane Doe",
        title="Chief Financial Officer",
        is_board_director=None,
        transaction_date="2026-10-15",
        transaction_shares=10,
        transaction_price_per_share=200,
        transaction_value=2000,
        shares_owned_before_transaction=None,
        shares_owned_after_transaction=10,
        security_title="Common Stock",
        filing_date="2026-10-16",
    )
    monkeypatch.setattr(api_free, "get_prices", lambda *args, **kwargs: [])
    monkeypatch.setattr(api_free, "get_financial_metrics", lambda *args, **kwargs: [])
    monkeypatch.setattr(api_free, "get_company_news", lambda *args, **kwargs: [])
    monkeypatch.setattr(api_free, "get_insider_trades", lambda *args, **kwargs: [trade])

    data = gather_data.get_ticker_data_swing("AAPL")

    assert data["insider_trades"] == [{
        "name": "Jane Doe",
        "type": None,
        "shares": 10.0,
        "price": 200.0,
        "date": "2026-10-15",
    }]
