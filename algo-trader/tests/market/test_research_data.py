import pandas as pd

from market import research_data as api_free


def test_get_prices_uses_yfinance_and_caches_results(monkeypatch):
    history = pd.DataFrame(
        [{"Open": 100, "Close": 101, "High": 102, "Low": 99, "Volume": 1_000}],
        index=pd.to_datetime(["2024-01-01"]),
    )

    class FakeTicker:
        def history(self, **kwargs):
            assert kwargs == {"start": "2024-01-01", "end": "2024-01-03", "interval": "1d"}
            return history

    monkeypatch.setattr(api_free._cache, "get_prices", lambda key: None)
    monkeypatch.setattr(api_free._cache, "set_prices", lambda key, value: cached.append((key, value)))
    monkeypatch.setattr(api_free, "_disk_cache_get", lambda *args, **kwargs: None)
    monkeypatch.setattr(api_free, "_disk_cache_set", lambda *args, **kwargs: None)
    monkeypatch.setattr(api_free, "_get_yf_ticker", lambda ticker: FakeTicker())
    cached = []

    prices = api_free.get_prices("AAPL", "2024-01-01", "2024-01-02")

    assert len(prices) == 1
    assert prices[0].open == 100
    assert prices[0].close == 101
    assert prices[0].time == "2024-01-01T00:00:00Z"
    assert cached[0][0] == "AAPL_2024-01-01_2024-01-02"


def test_get_prices_returns_cached_results_without_network(monkeypatch):
    monkeypatch.setattr(
        api_free._cache,
        "get_prices",
        lambda key: [{
            "open": 100, "close": 101, "high": 102, "low": 99,
            "volume": 1_000, "time": "2024-01-01T00:00:00Z",
        }],
    )
    monkeypatch.setattr(api_free, "_get_yf_ticker", lambda *_: (_ for _ in ()).throw(AssertionError("cache miss")))

    prices = api_free.get_prices("AAPL", "2024-01-01", "2024-01-02")

    assert len(prices) == 1
    assert prices[0].close == 101


def test_get_company_news_uses_yahoo_search_and_caches_articles(monkeypatch):
    timestamp = 1793275200  # 2026-10-29T12:00:00Z
    cached = []

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"news": [{
                "title": "AAPL headline",
                "publisher": "Example News",
                "link": "https://example.test/aapl",
                "providerPublishTime": timestamp,
            }]}

    monkeypatch.setattr(api_free._cache, "get_company_news", lambda key: None)
    monkeypatch.setattr(api_free._cache, "set_company_news", lambda key, value: cached.append((key, value)))
    monkeypatch.setattr(api_free, "_disk_cache_get", lambda *args, **kwargs: None)
    monkeypatch.setattr(api_free, "_disk_cache_set", lambda *args, **kwargs: None)
    monkeypatch.setattr(api_free.requests, "get", lambda *args, **kwargs: FakeResponse())
    monkeypatch.setattr(api_free, "_get_yf_ticker", lambda *_: (_ for _ in ()).throw(AssertionError("search endpoint should be used")))

    articles = api_free.get_company_news("AAPL", "2026-10-29", limit=100)

    assert len(articles) == 1
    assert articles[0].ticker == "AAPL"
    assert articles[0].title == "AAPL headline"
    assert articles[0].date == "2026-10-29T12:00:00Z"
    assert cached[0][0] == "AAPL_none_2026-10-29_100"
