"""Economic calendar feed, used by the hard news blackout and shown to the macro analyst."""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx

from .models import CalendarEvent

ALL_CURRENCIES = ["USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD"]


class FeedCalendar:
    """Reads a weekly JSON calendar (the Forex Factory export format) and refreshes it hourly.

    If the feed can't be read and fail_closed is set, every currency gets a high-impact
    placeholder event, so the news blackout blocks new trades instead of silently passing them.
    """

    def __init__(self, url: str, fail_closed: bool, http: httpx.Client | None = None):
        self.url = url
        self.fail_closed = fail_closed
        self.http = http or httpx.Client(timeout=20)
        self.cached: list[CalendarEvent] = []
        self.fetched_at: datetime | None = None
        self.last_error: str | None = None

    def _fetch(self) -> list[CalendarEvent]:
        resp = self.http.get(self.url)
        resp.raise_for_status()
        events = []
        for row in resp.json():
            impact = str(row.get("impact", "")).lower()
            if impact not in ("high", "medium", "low"):
                continue
            events.append(CalendarEvent(
                time=datetime.fromisoformat(row["date"]),
                currency=row["country"],
                title=row["title"],
                impact=impact,
            ))
        return events

    def events(self, now: datetime) -> list[CalendarEvent]:
        if self.fetched_at is None or now - self.fetched_at > timedelta(hours=1):
            try:
                self.cached = self._fetch()
                self.fetched_at = now
                self.last_error = None
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                self.last_error = f"Economic calendar unavailable: {exc}"
                if self.fail_closed:
                    return [CalendarEvent(now, c, "Economic calendar unavailable", "high") for c in ALL_CURRENCIES]
        return self.cached

    def upcoming(self, currencies: set[str], now: datetime, hours: int = 48) -> list[CalendarEvent]:
        end = now + timedelta(hours=hours)
        return [
            e for e in self.events(now)
            if e.currency in currencies and e.impact != "low" and now - timedelta(hours=2) <= e.time <= end
        ]
