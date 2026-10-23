import json
from datetime import date, datetime, timedelta, timezone

import httpx

from stock_fakes import NOW, FakeAlpaca, FakeEdgar, FakeNews, bars, filing, settings
from tradeboy.journal import Journal
from tradeboy.stocks.alpaca import AlpacaPaper
from tradeboy.stocks.events import EventScanner
from tradeboy.stocks.sec import Edgar, html_to_text


def scanner(edgar=None, broker=None, news=None):
    return EventScanner(settings(), broker or FakeAlpaca(), Journal(":memory:"), edgar or FakeEdgar(), news or FakeNews())


def test_earnings_8k_outranks_other_filings_and_routine_8ks_are_ignored():
    edgar = FakeEdgar([
        filing("NVDA", items=("2.02", "9.01")),
        filing("AMD", items=("5.07",), accession="a2"),  # shareholder vote: not material for price
        filing("MSFT", form="10-Q", items=(), accession="a3"),
    ])
    found = scanner(edgar).scan(NOW)
    assert [(e.symbol, e.kind) for e in found] == [("NVDA", "earnings"), ("MSFT", "filing")]


def test_old_filings_are_skipped():
    edgar = FakeEdgar([filing(accepted=NOW - timedelta(days=3))])
    assert scanner(edgar).scan(NOW) == []


def test_big_move_against_the_benchmark():
    broker = FakeAlpaca()
    broker.daily["AMD"] = bars(start=100.0, step=0.0, wiggle=1.0)  # flat, about 0.6% a day of noise
    broker.price["AMD"] = 110.0
    found = scanner(broker=broker).scan(NOW)
    assert [(e.symbol, e.kind) for e in found] == [("AMD", "big_move")]
    assert "+10" in found[0].summary or "+9.9" in found[0].summary


def test_news_burst():
    news = FakeNews({"MSFT": [f"story {i}" for i in range(9)]})
    found = scanner(news=news).scan(NOW)
    assert [(e.symbol, e.kind) for e in found] == [("MSFT", "news_burst")]


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_edgar_reads_filings_and_picks_the_press_release():
    sub = {"filings": {"recent": {
        "form": ["8-K", "4", "10-Q"], "filingDate": ["2026-10-22", "2026-10-21", "2026-08-27"],
        "acceptanceDateTime": ["2026-10-22T20:05:11.000Z", "2026-10-21T20:00:00.000Z", "2026-08-27T20:00:00.000Z"],
        "accessionNumber": ["0001045810-26-000100", "x", "y"], "items": ["2.02,9.01", "", ""],
        "primaryDocument": ["nvda-8k.htm", "f4.xml", "q.htm"]}}}
    seen = []

    def handler(request):
        seen.append(request)
        path = request.url.path
        if path.endswith("company_tickers.json"):
            return httpx.Response(200, json={"0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA"}})
        if path.endswith("CIK0001045810.json"):
            return httpx.Response(200, json=sub)
        if path.endswith("index.json"):
            return httpx.Response(200, json={"directory": {"item": [{"name": "nvda-8k.htm"}, {"name": "q3fy27pr-ex99_1.htm"}]}})
        if path.endswith("ex99_1.htm"):
            return httpx.Response(200, text="<html><p>Revenue&nbsp;was $57.0 billion</p></html>")
        return httpx.Response(404)

    edgar = Edgar("Trade-Boy test test@example.com", http=_client(handler))
    filings = edgar.filings("NVDA", NOW.replace(day=23), days=30)
    assert [(f.form, f.items) for f in filings] == [("8-K", ("2.02", "9.01"))]
    assert filings[0].accepted == datetime(2026, 10, 22, 20, 5, 11, tzinfo=timezone.utc)
    assert "Revenue was $57.0 billion" in edgar.filing_text(filings[0])


def test_edgar_failure_is_a_data_gap_not_a_crash():
    edgar = Edgar("ua", http=_client(lambda r: httpx.Response(403)))
    assert edgar.filings("NVDA", NOW) == []
    assert "HTTP 403" in edgar.gaps("NVDA")[0]


def test_html_to_text():
    assert html_to_text("<div>A</div><script>x</script><p>B&amp;C</p>") == "A\nB&C"


def test_alpaca_is_paper_only_and_sends_keys_and_orders_correctly():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/v2/stocks/quotes/latest":
            return httpx.Response(200, json={"quotes": {"NVDA": {"bp": 0, "ap": 124.1, "t": "2026-10-22T15:00:00.123456789Z"}}})
        if request.url.path == "/v2/stocks/trades/latest":
            return httpx.Response(200, json={"trades": {"NVDA": {"p": 124.0, "t": "2026-10-22T15:00:00Z"}}})
        if request.url.path == "/v2/orders":
            return httpx.Response(200, json={"id": "o1", **json.loads(request.content)})
        return httpx.Response(404)

    client = AlpacaPaper("key", "secret", http=_client(handler))
    q = client.quotes(["NVDA"])["NVDA"]
    assert q.bid == q.ask == 124.0  # one-sided IEX quote: fall back on the last trade
    order = client.submit_order("NVDA", 0.0806, "buy")
    assert order["qty"] == "0.0806" and order["type"] == "market"
    assert client.submit_order("NVDA", 3.0, "sell", "stop", stop_price=117.456, time_in_force="gtc")["stop_price"] == "117.46"
    hosts = {r.url.host for r in requests}
    assert hosts <= {"paper-api.alpaca.markets", "data.alpaca.markets"}
    assert all(r.headers["APCA-API-KEY-ID"] == "key" for r in requests)


def test_alpaca_needs_keys(monkeypatch):
    monkeypatch.delenv("TRADEBOY_ALPACA_KEY_ID", raising=False)
    try:
        AlpacaPaper()
    except RuntimeError as exc:
        assert "TRADEBOY_ALPACA_KEY_ID" in str(exc)
    else:
        raise AssertionError("expected an error")


def test_yahoo_earnings_date_from_calendar():
    from tradeboy.stocks.yahoo import YahooFinance

    class Ticker:
        def __init__(self, symbol):
            self.calendar = {"Earnings Date": [date(2026, 11, 19), date(2026, 11, 24)]}

    y = YahooFinance(ticker_factory=Ticker)
    assert y.next_earnings("NVDA", NOW) == date(2026, 11, 19)

    class Broken:
        def __init__(self, symbol):
            raise ValueError("rate limited")

    y2 = YahooFinance(ticker_factory=Broken)
    assert y2.next_earnings("NVDA", NOW) is None and "rate limited" in y2.errors["NVDA"]
