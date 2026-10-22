from datetime import datetime, timezone

import httpx

from tradeboy.calendar import FeedCalendar
from tradeboy.risk import in_news_blackout

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
FEED = [
    {"title": "CPI m/m", "country": "USD", "date": "2026-10-22T08:10:00-04:00", "impact": "High"},
    {"title": "Bank Holiday", "country": "JPY", "date": "2026-10-22T00:00:00-04:00", "impact": "Holiday"},
    {"title": "Retail Sales", "country": "EUR", "date": "2026-10-23T05:00:00-04:00", "impact": "Medium"},
]


def calendar(handler, fail_closed=False):
    return FeedCalendar("https://feed.test/cal.json", fail_closed,
                        http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_parses_feed_and_drives_the_blackout():
    cal = calendar(lambda r: httpx.Response(200, json=FEED))
    events = cal.events(NOW)
    assert [e.title for e in events] == ["CPI m/m", "Retail Sales"]
    assert in_news_blackout("EUR_USD", events, NOW, 15).title == "CPI m/m"
    assert [e.title for e in cal.upcoming({"EUR"}, NOW)] == ["Retail Sales"]


def test_unreachable_feed_fails_closed_when_trading():
    cal = calendar(lambda r: httpx.Response(503), fail_closed=True)
    assert in_news_blackout("USD_JPY", cal.events(NOW), NOW, 15) is not None
    assert "unavailable" in cal.last_error


def test_unreachable_feed_fails_open_in_dry_run():
    cal = calendar(lambda r: httpx.Response(503))
    assert cal.events(NOW) == []
