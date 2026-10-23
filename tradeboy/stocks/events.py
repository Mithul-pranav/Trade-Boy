"""The event scanner: free code that decides when a stock is worth a (paid) debate.

Events, highest priority first:
- earnings: an 8-K with item 2.02 (results of operations), the company's own press release;
- filing: a 10-Q, 10-K or an 8-K with a material item (deals, impairments, executive changes, ...);
- big_move: today's move against the benchmark is both large and unusual for the stock;
- news_burst: many headlines on the stock in the last few hours.

Each event has a key and is debated at most once. Filings older than MAX_FILING_AGE are skipped, so a
desk starting for the first time doesn't debate last month's reports.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from .market import move_today, ny_date
from .models import StockEvent
from .sec import describe

log = logging.getLogger("tradeboy")

MAX_FILING_AGE = timedelta(hours=36)
NEWS_WINDOW_HOURS = 3
PRIORITY = {"earnings": 4, "filing": 3, "big_move": 2, "news_burst": 1}


class EventScanner:
    def __init__(self, settings, broker, journal, edgar=None, news=None):
        self.settings = settings
        self.rules = settings.events
        self.broker = broker
        self.journal = journal
        self.edgar = edgar
        self.news = news

    def seen(self, key: str) -> bool:
        return self.journal.get_state(f"event:{key}") is not None

    def mark_seen(self, event: StockEvent, now: datetime, outcome: str = "debated") -> None:
        self.journal.set_state(f"event:{event.key}", f"{now.isoformat()} {outcome}")

    def scan(self, now: datetime, symbols: list[str] | None = None) -> list[StockEvent]:
        """All new events, highest priority first, at most one per stock (its most important)."""
        symbols = symbols or self.settings.symbols
        found: list[StockEvent] = []
        found += self._filings(symbols, now)
        try:
            found += self._moves(symbols, now)
        except Exception as exc:  # prices can fail without stopping filings and news
            log.error("Big-move scan failed: %s", exc)
        found += self._news(symbols, now)
        found = [e for e in found if not self.seen(e.key)]
        best: dict[str, StockEvent] = {}
        for e in sorted(found, key=lambda e: -e.priority):
            best.setdefault(e.symbol, e)
        return sorted(best.values(), key=lambda e: -e.priority)

    def related(self, symbol: str, now: datetime) -> list[StockEvent]:
        """Every new event on one stock: all of them are covered by its debate and marked seen with it."""
        return [e for e in self._filings([symbol], now) + self._news([symbol], now) if not self.seen(e.key)]

    def _filings(self, symbols: list[str], now: datetime) -> list[StockEvent]:
        if self.edgar is None:
            return []
        out = []
        for symbol in symbols:
            for f in self.edgar.filings(symbol, now, self.rules.filing_forms, days=3):
                when = f.accepted or datetime.combine(f.filed, datetime.min.time(), tzinfo=now.tzinfo)
                if now - when > MAX_FILING_AGE:
                    continue
                if f.form == "8-K":
                    material = [i for i in f.items if i in self.rules.material_8k_items]
                    if not material:
                        continue
                    kind = "earnings" if "2.02" in material else "filing"
                else:
                    kind = "filing"
                out.append(StockEvent(symbol, kind, PRIORITY[kind], f"{self.settings.name(symbol)}: {describe(f)}",
                                      f"filing:{f.accession}", f))
        return out

    def _moves(self, symbols: list[str], now: datetime) -> list[StockEvent]:
        bench = self.settings.benchmark
        quotes = self.broker.quotes(symbols + [bench])
        if bench not in quotes:
            return []
        bench_bars = self.broker.bars(bench, "1Day", 90)
        today = ny_date(now)
        out = []
        for symbol in symbols:
            if symbol not in quotes:
                continue
            m = move_today(self.broker.bars(symbol, "1Day", 90), bench_bars, quotes[symbol].mid, quotes[bench].mid, today)
            if not m or m["excess_sigma"] is None:
                continue
            if abs(m["excess_pct"]) >= self.rules.big_move_pct and abs(m["excess_sigma"]) >= self.rules.big_move_sigma:
                out.append(StockEvent(
                    symbol, "big_move", PRIORITY["big_move"],
                    f"{self.settings.name(symbol)} is {m['move_pct']:+.1f}% today, {m['excess_pct']:+.1f}% against "
                    f"{bench} ({m['excess_sigma']:+.1f} standard deviations)",
                    f"move:{symbol}:{today.isoformat()}",
                ))
        return out

    def _news(self, symbols: list[str], now: datetime) -> list[StockEvent]:
        if self.news is None:
            return []
        out = []
        for symbol in symbols:
            recent = self.news.headlines({symbol}, now, hours=NEWS_WINDOW_HOURS, limit=50)
            if len(recent) >= self.rules.news_burst_headlines:
                block = now.hour // NEWS_WINDOW_HOURS
                out.append(StockEvent(
                    symbol, "news_burst", PRIORITY["news_burst"],
                    f"{len(recent)} headlines on {self.settings.name(symbol)} in {NEWS_WINDOW_HOURS} hours, latest: "
                    f"{recent[0].title[:140]}",
                    f"news:{symbol}:{ny_date(now).isoformat()}:{block}",
                ))
        return out
