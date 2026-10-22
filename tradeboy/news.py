"""Free news headlines from RSS/Atom feeds (central banks and FX news sites), shown to the macro analyst.

Fetched in code, so the macro analyst has dated news even when Claude's web search is unavailable.
A feed that can't be read is reported to the agents as a data gap rather than silently skipped.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

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


@dataclass(frozen=True)
class Feed:
    url: str
    name: str
    # Currencies every item concerns (a central bank's feed). Empty: a general feed, filtered by keyword.
    currencies: tuple[str, ...] = ()


@dataclass(frozen=True)
class Headline:
    time: datetime
    source: str
    title: str
    currencies: tuple[str, ...]


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


def parse_feed(xml: str) -> list[tuple[datetime, str]]:
    """(time, title) for each RSS item or Atom entry that has both."""
    root = ET.fromstring(xml)
    items = root.iter("item") if root.find(".//item") is not None else root.iter(f"{ATOM}entry")
    out = []
    for item in items:
        title = _text(item, "title", f"{ATOM}title")
        when = _when(_text(item, "pubDate", "{http://purl.org/dc/elements/1.1/}date", f"{ATOM}updated", f"{ATOM}published"))
        if title and when:
            out.append((when, re.sub(r"\s+", " ", title).strip()))
    return out


def mentions(title: str, currency: str) -> bool:
    text = title.lower()
    return any(re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", text) for word in KEYWORDS.get(currency, [currency.lower()]))


class NewsFeeds:
    """Reads the configured feeds, at most every 15 minutes, and keeps the last good copy of each."""

    def __init__(self, feeds: list[Feed], http: httpx.Client | None = None, refresh_minutes: int = 15):
        self.feeds = feeds
        self.http = http or httpx.Client(timeout=15, follow_redirects=True,
                                         headers={"User-Agent": "Trade-Boy/0.2 (news reader)"})
        self.refresh = timedelta(minutes=refresh_minutes)
        self.cached: dict[str, list[tuple[datetime, str]]] = {}
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
        found = []
        for feed in self.feeds:
            for when, title in self.cached.get(feed.url, []):
                if not since <= when <= now + timedelta(minutes=5):
                    continue
                tags = tuple(c for c in sorted(currencies) if c in feed.currencies or (not feed.currencies and mentions(title, c)))
                if tags:
                    found.append(Headline(when, feed.name, title, tags))
        found.sort(key=lambda h: h.time, reverse=True)
        return found[:limit]

    def gaps(self) -> list[str]:
        return [f"News feed '{name}' unreachable: {err}" for name, err in sorted(self.errors.items())]
