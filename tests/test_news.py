from datetime import datetime, timezone

import httpx

from fakes import FakeBroker, FakeLLM, agreeing_script

from tradeboy.config import load_settings
from tradeboy.debate import Debate, gather_evidence
from tradeboy.news import Feed, NewsFeeds, parse_feed

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
FED_RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Fed</title>
<item><title>Federal Reserve issues FOMC statement</title><pubDate>Wed, 21 Oct 2026 18:00:00 GMT</pubDate></item>
<item><title>Old minutes</title><pubDate>Mon, 05 Oct 2026 18:00:00 GMT</pubDate></item>
</channel></rss>"""
FX_ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>FX</title>
<entry><title>Yen jumps as BoJ hints at hike</title><updated>2026-10-22T09:30:00Z</updated></entry>
<entry><title>Sterling slips after UK CPI</title><updated>2026-10-22T08:00:00Z</updated></entry>
</feed>"""
FEEDS = [Feed("https://fed.test/rss", "Fed", ("USD",)), Feed("https://fx.test/atom", "FX news")]


def feeds(fail=()):
    def handler(request):
        if request.url.host in fail:
            return httpx.Response(503)
        return httpx.Response(200, text=FED_RSS if "fed" in request.url.host else FX_ATOM)
    return NewsFeeds(FEEDS, http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_parses_rss_and_atom():
    assert parse_feed(FED_RSS)[0][1] == "Federal Reserve issues FOMC statement"
    assert parse_feed(FX_ATOM)[0][0] == datetime(2026, 10, 22, 9, 30, tzinfo=timezone.utc)


def test_tags_central_bank_items_and_matches_general_feeds_by_keyword():
    news = feeds()
    jpy = news.headlines({"USD", "JPY"}, NOW)
    assert [h.title for h in jpy] == ["Yen jumps as BoJ hints at hike", "Federal Reserve issues FOMC statement"]
    assert jpy[0].currencies == ("JPY",)
    # Old items fall outside the window; the sterling story doesn't concern EUR/CHF.
    assert news.headlines({"EUR", "CHF"}, NOW) == []


def test_unreachable_feed_is_reported_as_a_gap_and_keeps_the_rest():
    news = feeds(fail={"fx.test"})
    assert [h.source for h in news.headlines({"USD", "JPY"}, NOW)] == ["Fed"]
    assert news.gaps() and "FX news" in news.gaps()[0]


def test_macro_analyst_sees_headlines_and_data_gaps():
    evidence = gather_evidence(FakeBroker(), None, "EUR_USD", NOW, news=feeds(fail={"fx.test"}))
    assert evidence.news[0]["headline"] == "Federal Reserve issues FOMC statement"
    assert any("calendar" in g for g in evidence.data_gaps) and any("FX news" in g for g in evidence.data_gaps)
    llm = FakeLLM(agreeing_script())
    Debate(load_settings(), llm).run(evidence, FakeBroker().account(), NOW)
    macro = next(c["prompt"] for c in llm.calls if c["agent"] == "macro")
    assert "FOMC statement" in macro and "Missing data" in macro
