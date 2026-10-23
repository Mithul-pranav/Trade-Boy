"""Yahoo Finance through the yfinance package: long daily price history and earnings dates.

Live quotes and orders come from Alpaca; Yahoo gives the decades of daily bars the research needs and
the scheduled earnings dates behind the earnings blackout. Its answers are cached for a few hours.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from .models import Bar

log = logging.getLogger("tradeboy")


def _as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if hasattr(value, "to_pydatetime"):  # pandas Timestamp
        return value.to_pydatetime().date()
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


class YahooFinance:
    def __init__(self, refresh_hours: float = 6, ticker_factory=None):
        if ticker_factory is None:
            import yfinance

            ticker_factory = yfinance.Ticker
        self.ticker = ticker_factory
        self.refresh = timedelta(hours=refresh_hours)
        self._earnings: dict[str, tuple[datetime, date | None]] = {}
        self.errors: dict[str, str] = {}

    def daily_bars(self, symbol: str, start: date, end: date | None = None) -> list[Bar]:
        """Split- and dividend-adjusted daily bars."""
        frame = self.ticker(symbol).history(start=start.isoformat(), end=(end or date.today() + timedelta(days=1)).isoformat(),
                                            interval="1d", auto_adjust=True, actions=False)
        bars = []
        for when, row in frame.iterrows():
            t = when.to_pydatetime()
            t = t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)
            bars.append(Bar(t, float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"]),
                            float(row["Volume"])))
        return bars

    def next_earnings(self, symbol: str, now: datetime) -> date | None:
        """The next scheduled earnings date on or after today, or None if Yahoo doesn't list one."""
        cached = self._earnings.get(symbol)
        if cached and now - cached[0] < self.refresh:
            return cached[1]
        found = None
        try:
            calendar = self.ticker(symbol).calendar or {}
            dates = calendar.get("Earnings Date") if isinstance(calendar, dict) else None
            upcoming = sorted(d for d in (_as_date(x) for x in (dates or [])) if d and d >= now.date())
            found = upcoming[0] if upcoming else None
            self.errors.pop(symbol, None)
        except Exception as exc:  # yfinance raises many kinds; a missing date is a data gap, not a crash
            self.errors[symbol] = f"Yahoo earnings date for {symbol} unavailable: {str(exc)[:120] or type(exc).__name__}"
            log.warning("%s", self.errors[symbol])
            if cached:
                return cached[1]
        self._earnings[symbol] = (now, found)
        return found

    def past_earnings(self, symbol: str, limit: int = 60) -> list[date]:
        """Past earnings dates (most recent first), for research."""
        frame = self.ticker(symbol).get_earnings_dates(limit=limit)
        if frame is None:
            return []
        return sorted({d for d in (_as_date(x) for x in frame.index) if d and d <= date.today()}, reverse=True)
