import json

import httpx

from tradeboy.config import OandaCredentials, load_settings
from tradeboy.oanda import OandaClient

CREDS = OandaCredentials(token="t", account_id="101-003-1-001")


def client(handler):
    http = httpx.Client(base_url=CREDS.base_url, transport=httpx.MockTransport(handler))
    return OandaClient(CREDS, http=http)


def test_practice_url_by_default():
    assert CREDS.base_url == "https://api-fxpractice.oanda.com"


def test_quotes_use_home_conversion_for_quote_currency():
    def handler(request):
        assert request.url.params["includeHomeConversions"] == "true"
        return httpx.Response(200, json={
            "prices": [{"instrument": "USD_JPY", "bids": [{"price": "150.010"}], "asks": [{"price": "150.025"}]}],
            "homeConversions": [
                {"currency": "JPY", "accountGain": "0.0089", "accountLoss": "0.0091", "positionValue": "0.009"},
                {"currency": "USD", "accountGain": "1.34", "accountLoss": "1.36", "positionValue": "1.35"},
            ],
        })

    q = client(handler).quotes(["USD_JPY"])["USD_JPY"]
    assert q.quote_to_home_loss == 0.0091
    assert round(q.spread_pips, 1) == 1.5


def test_market_order_sends_stop_and_target_with_pair_precision():
    sent = {}

    def handler(request):
        sent.update(json.loads(request.content))
        return httpx.Response(201, json={"orderFillTransaction": {"price": "150.02", "tradeOpened": {"tradeID": "9"}}})

    client(handler).market_order("USD_JPY", -1000, 150.5, 149.25)
    order = sent["order"]
    assert order["units"] == "-1000"
    assert order["stopLossOnFill"] == {"price": "150.500"}
    assert order["takeProfitOnFill"] == {"price": "149.250"}
    assert order["timeInForce"] == "FOK"


def test_to_home_rate_reads_position_value():
    def handler(request):
        return httpx.Response(200, json={"prices": [], "homeConversions": [
            {"currency": "USD", "accountGain": "1.34", "accountLoss": "1.36", "positionValue": "1.35"},
        ]})

    assert client(handler).to_home_rate("USD", "EUR_USD") == 1.35


def test_candles_skip_incomplete_and_parse_nanoseconds():
    def handler(request):
        return httpx.Response(200, json={"candles": [
            {"time": "2026-10-22T08:00:00.000000000Z", "complete": True, "volume": 10,
             "mid": {"o": "1.1", "h": "1.2", "l": "1.0", "c": "1.15"}},
            {"time": "2026-10-22T08:15:00.000000000Z", "complete": False, "volume": 3,
             "mid": {"o": "1.15", "h": "1.16", "l": "1.14", "c": "1.15"}},
        ]})

    candles = client(handler).candles("EUR_USD")
    assert len(candles) == 1
    assert candles[0].time.hour == 8 and candles[0].close == 1.15


def test_settings_file_loads_defaults():
    s = load_settings()
    assert len(s.pairs) == 7
    assert s.risk.risk_per_trade_pct == 0.5
    assert s.risk.max_drawdown_pct is None
    assert (s.starting_capital, s.capital_currency) == (100.0, "USD")


def test_without_token_sends_no_auth_and_finds_the_account():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"accounts": [{"id": "101-003-9-001", "tags": []}]})

    from tradeboy.config import OandaCredentials as Creds
    creds = Creds(token="", account_id="")
    oanda = OandaClient(creds, http=httpx.Client(base_url=creds.base_url, transport=httpx.MockTransport(handler)))
    assert oanda.account_id == "101-003-9-001"
    assert seen[0].url.path == "/v3/accounts"


def test_position_book_shares_and_trapped_traders():
    def handler(request):
        return httpx.Response(200, json={"positionBook": {"time": "t", "price": "1.1000", "buckets": [
            {"price": "1.0900", "longCountPercent": "30", "shortCountPercent": "10"},
            {"price": "1.1100", "longCountPercent": "40", "shortCountPercent": "20"},
        ]}})

    book = client(handler).position_book("EUR_USD")
    assert (book["retail_long_pct"], book["retail_short_pct"]) == (70.0, 30.0)
    assert book["longs_underwater_pct"] == 40.0  # bought above the current price
    assert book["shorts_underwater_pct"] == 10.0  # sold below it
