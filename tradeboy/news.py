"""Free news headlines from RSS/Atom feeds (central banks, FX news sites and Google News searches),
shown to the macro analyst.

Fetched in code, so the macro analyst has dated news even when Claude's web search is unavailable.
A feed that can't be read is reported to the agents as a data gap rather than silently skipped.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import NamedTuple
from urllib.parse import urlencode

import httpx

# Words that tie a headline from a general FX feed to a currency.
KEYWORDS = {
    "USD": ["usd", "dollar", "fed", "fomc", "powell", "u.s.", "us", "treasury", "nfp", "payrolls"],
    "EUR": ["eur", "euro", "ecb", "lagarde", "eurozone", "euro area", "germany", "german"],
    "GBP": ["gbp", "pound", "sterling", "boe", "bank of england", "bailey", "uk", "u.k.", "britain"],
    "JPY": ["jpy", "yen", "boj", "bank of japan", "ueda", "japan"],
    "AUD": ["aud", "aussie", "rba", "australia"],
    "CAD": ["cad", "loonie", "boc", "bank of canada", "canada", "oil"],
    "CHF": ["chf", "franc", "snb", "swiss"],
    "NZD": ["nzd", "kiwi", "rbnz", "new zealand"],
}
ATOM = "{http://www.w3.org/2005/Atom}"


GOOGLE_NEWS = "https://news.google.com/rss/search"


def google_news_url(query: str, days: int = 2) -> str:
    """Google News RSS search (no key needed), limited to stories from the last few days."""
    return GOOGLE_NEWS + "?" + urlencode({"q": f"{query} when:{days}d", "hl": "en-US", "gl": "US", "ceid": "US:en"})


@dataclass(frozen=True)
class Feed:
    url: str
    name: str
    # Currencies every item concerns (a central bank's feed). Empty: a general feed, filtered by keyword.
    currencies: tuple[str, ...] = ()
    # Only the newest few matching items of each feed, so a busy search feed can't crowd out the rest.
    max_items: int = 10

    @classmethod
    def from_config(cls, raw: dict) -> "Feed":
        """A settings entry: {name, url | google_news: query, currencies, max_items}."""
        url = raw.get("url") or google_news_url(raw["google_news"])
        return cls(url, raw.get("name", url), tuple(raw.get("currencies", ())), int(raw.get("max_items", 10)))


class Item(NamedTuple):
    time: datetime
    title: str
    publisher: str = ""
    link: str = ""


@dataclass(frozen=True)
class Headline:
    time: datetime
    source: str
    title: str
    currencies: tuple[str, ...]
    publisher: str = ""
    link: str = ""
    # FinBERT's read of the headline's tone, e.g. {"label": "negative", "confidence": 0.93}, when enabled.
    sentiment: dict | None = None


def _when(text: str | None) -> datetime | None:
    if not text:
        return None
    text = text.strip()
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            when = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _text(node, *tags) -> str | None:
    for tag in tags:
        found = node.find(tag)
        if found is not None and found.text:
            return found.text
    return None


def _link(item) -> str:
    link = item.find("link")
    if link is not None and link.text:
        return link.text.strip()
    atom = item.find(f"{ATOM}link")
    return atom.get("href", "") if atom is not None else ""


def parse_feed(xml: str) -> list[Item]:
    """Each RSS item or Atom entry that has a title and a date.

    Google News titles end in " - Publisher" and name the publisher in <source>; the suffix is dropped.
    """
    root = ET.fromstring(xml)
    items = root.iter("item") if root.find(".//item") is not None else root.iter(f"{ATOM}entry")
    out = []
    for item in items:
        title = _text(item, "title", f"{ATOM}title")
        when = _when(_text(item, "pubDate", "{http://purl.org/dc/elements/1.1/}date", f"{ATOM}updated", f"{ATOM}published"))
        if not (title and when):
            continue
        title = re.sub(r"\s+", " ", title).strip()
        publisher = (_text(item, "source") or "").strip()
        if publisher and title.endswith(f" - {publisher}"):
            title = title[: -len(publisher) - 3].rstrip()
        out.append(Item(when, title, publisher, _link(item)))
    return out


def mentions(title: str, currency: str) -> bool:
    text = title.lower()
    return any(re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", text) for word in KEYWORDS.get(currency, [currency.lower()]))


class NewsFeeds:
    """Reads the configured feeds, at most every 15 minutes, and keeps the last good copy of each.

    With a sentiment scorer, each headline is scored once and the score is kept with it.
    """

    def __init__(self, feeds: list[Feed], http: httpx.Client | None = None, refresh_minutes: int = 15,
                 sentiment=None):
        self.feeds = feeds
        self.http = http or httpx.Client(timeout=15, follow_redirects=True,
                                         headers={"User-Agent": "Trade-Boy/0.2 (news reader)"})
        self.refresh = timedelta(minutes=refresh_minutes)
        self.sentiment = sentiment
        self.cached: dict[str, list[Item]] = {}
        self.fetched_at: dict[str, datetime] = {}
        self.errors: dict[str, str] = {}

    def _refresh(self, now: datetime) -> None:
        for feed in self.feeds:
            last = self.fetched_at.get(feed.url)
            if last is not None and now - last < self.refresh:
                continue
            try:
                resp = self.http.get(feed.url)
                resp.raise_for_status()
                self.cached[feed.url] = parse_feed(resp.text)
                self.errors.pop(feed.name, None)
            except (httpx.HTTPError, ET.ParseError) as exc:
                self.errors[feed.name] = str(exc)[:150] or type(exc).__name__
            self.fetched_at[feed.url] = now

    def headlines(self, currencies: set[str], now: datetime, hours: int = 48, limit: int = 25) -> list[Headline]:
        self._refresh(now)
        since = now - timedelta(hours=hours)
        found: dict[str, Headline] = {}  # by title, so a story in several feeds is listed once
        for feed in self.feeds:
            items = sorted(self.cached.get(feed.url, []), key=lambda i: i.time, reverse=True)
            kept = 0
            for item in items:
                if kept >= feed.max_items:
                    break
                if not since <= item.time <= now + timedelta(minutes=5):
                    continue
                tags = tuple(c for c in sorted(currencies)
                             if c in feed.currencies or (not feed.currencies and mentions(item.title, c)))
                if not tags:
                    continue
                kept += 1
                key = item.title.lower()
                if key in found:
                    seen = found[key]
                    found[key] = Headline(seen.time, seen.source, seen.title,
                                          tuple(sorted(set(seen.currencies) | set(tags))), seen.publisher, seen.link)
                else:
                    found[key] = Headline(item.time, feed.name, item.title, tags, item.publisher, item.link)
        newest = sorted(found.values(), key=lambda h: h.time, reverse=True)[:limit]
        if self.sentiment is not None:
            scores = self.sentiment.score([h.title for h in newest])
            newest = [Headline(h.time, h.source, h.title, h.currencies, h.publisher, h.link, s)
                      for h, s in zip(newest, scores)]
        return newest

    def gaps(self, currencies: set[str] | None = None) -> list[str]:
        """Unreachable feeds; with currencies, only the general feeds and those for these currencies."""
        relevant = {f.name for f in self.feeds if not currencies or not f.currencies or set(f.currencies) & currencies}
        gaps = [f"News feed '{name}' unreachable: {err}" for name, err in sorted(self.errors.items()) if name in relevant]
        if self.sentiment is not None and getattr(self.sentiment, "last_error", None):
            gaps.append(self.sentiment.last_error)
        return gaps
