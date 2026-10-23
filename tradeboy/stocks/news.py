"""Company headlines: one Google News search per stock (no key), read with the forex desk's feed reader.

Each feed tags its items with the stock's symbol, so `headlines({"NVDA"}, now)` is Nvidia's news only.
"""

from __future__ import annotations

from ..news import Feed, NewsFeeds, google_news_url


def stock_feeds(universe: dict[str, dict], max_items: int = 15) -> list[Feed]:
    feeds = []
    for symbol, info in universe.items():
        name = info.get("name", symbol)
        query = f'"{name}" (stock OR shares OR earnings OR {symbol})'
        feeds.append(Feed(google_news_url(query, days=3), f"Google News {symbol}", (symbol,), max_items))
    return feeds


def stock_news(universe: dict[str, dict], http=None) -> NewsFeeds:
    return NewsFeeds(stock_feeds(universe), http=http, refresh_minutes=20)
