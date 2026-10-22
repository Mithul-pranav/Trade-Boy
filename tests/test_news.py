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


GOOGLE_RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>"ECB" - Google News</title>
<item><title>ECB holds rates as inflation cools - Reuters</title><link>https://news.google.com/rss/articles/abc</link>
<pubDate>Thu, 22 Oct 2026 10:00:00 GMT</pubDate><source url="https://www.reuters.com">Reuters</source></item>
<item><title>Yen jumps as BoJ hints at hike</title><pubDate>Thu, 22 Oct 2026 09:30:00 GMT</pubDate></item>
<item><title>Euro slips - FT</title><pubDate>Thu, 22 Oct 2026 07:00:00 GMT</pubDate><source url="https://ft.com">FT</source></item>
<item><title>Lagarde speech preview</title><pubDate>Thu, 22 Oct 2026 06:00:00 GMT</pubDate></item>
</channel></rss>"""


def test_google_news_titles_lose_the_publisher_suffix_and_keep_publisher_and_link():
    item = parse_feed(GOOGLE_RSS)[0]
    assert (item.title, item.publisher, item.link) == ("ECB holds rates as inflation cools", "Reuters",
                                                        "https://news.google.com/rss/articles/abc")


def test_google_news_feed_from_settings_builds_a_search_url():
    feed = Feed.from_config({"name": "GN EUR", "google_news": 'ECB OR "euro zone"', "currencies": ["EUR"]})
    assert feed.url.startswith("https://news.google.com/rss/search?q=ECB+OR+%22euro+zone%22+when%3A2d")
    assert feed.url.endswith("&hl=en-US&gl=US&ceid=US%3Aen") and feed.currencies == ("EUR",) and feed.max_items == 10
    # Every Google News entry in the shipped settings builds, and each has a currency.
    google = [f for f in load_settings().news_feeds if "google_news" in f]
    assert len(google) == 8 and all(Feed.from_config(f).currencies for f in google)


def _client(pages: dict):
    return httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=pages[r.url.host])))


def test_each_feed_is_capped_and_a_story_in_two_feeds_is_listed_once():
    news = NewsFeeds([Feed("https://gn.test/eur", "GN EUR", ("EUR",), max_items=2),
                      Feed("https://fx.test/atom", "FX news")],
                     http=_client({"gn.test": GOOGLE_RSS, "fx.test": FX_ATOM}))
    got = news.headlines({"EUR", "JPY"}, NOW)
    # GN EUR keeps its 2 newest; the yen story appears in both feeds but is listed once, with both tags.
    assert [h.title for h in got] == ["ECB holds rates as inflation cools", "Yen jumps as BoJ hints at hike"]
    assert got[1].source == "GN EUR" and got[1].currencies == ("EUR", "JPY")
    assert got[0].publisher == "Reuters"


class FakeFinBert:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, titles, truncation):
        self.calls.append(list(titles))
        if self.fail:
            raise OSError("can't reach huggingface.co")
        return [{"label": "Negative" if "slips" in t else "Positive", "score": 0.934} for t in titles]


def test_finbert_scores_each_headline_once_and_the_macro_prompt_shows_it():
    from tradeboy.sentiment import FinBert

    pipe = FakeFinBert()
    news = NewsFeeds(FEEDS, http=feeds().http, sentiment=FinBert(pipeline_factory=lambda: pipe))
    got = news.headlines({"GBP", "JPY"}, NOW)
    assert [h.sentiment for h in got] == [{"label": "positive", "confidence": 0.93},
                                          {"label": "negative", "confidence": 0.93}]
    news.headlines({"GBP", "JPY"}, NOW)
    assert len(pipe.calls) == 1  # cached, not re-scored
    evidence = gather_evidence(FakeBroker(), None, "GBP_USD", NOW, news=news)
    assert evidence.news[0]["finbert_tone"] == "negative 0.93"


def test_finbert_failure_leaves_headlines_unscored_and_reports_a_gap():
    from tradeboy.sentiment import FinBert

    news = NewsFeeds(FEEDS, http=feeds().http, sentiment=FinBert(pipeline_factory=lambda: FakeFinBert(fail=True)))
    got = news.headlines({"JPY"}, NOW)
    assert got and all(h.sentiment is None for h in got)
    assert any("FinBERT" in g and "huggingface" in g for g in news.gaps())


def test_gaps_for_a_pair_leave_out_feeds_for_other_currencies():
    news = feeds(fail={"fed.test", "fx.test"})
    news.headlines({"EUR"}, NOW)
    assert [g.split("'")[1] for g in news.gaps({"EUR", "GBP"})] == ["FX news"]
    assert [g.split("'")[1] for g in news.gaps({"USD"})] == ["FX news", "Fed"]
