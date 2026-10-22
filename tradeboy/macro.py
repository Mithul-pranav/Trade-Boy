"""Macro numbers from FRED's public CSV downloads (no key needed), shown to the macro analyst.

Rates, yields, inflation and jobs move currencies through rate differentials, so the analyst gets the
latest value of each series for the pair's currencies, its date, and how it has changed. Series are
refreshed once a day and the last good copy is kept; a series that can't be read is reported as a data
gap, never filled in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import httpx

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"


@dataclass(frozen=True)
class Series:
    id: str
    name: str
    currency: str
    # "level": the value as published. "yoy": percent change from a year earlier (for price indexes).
    kind: str = "level"

    @classmethod
    def from_config(cls, raw: dict) -> "Series":
        return cls(raw["id"], raw.get("name", raw["id"]), raw["currency"], raw.get("kind", "level"))


def parse_csv(text: str) -> list[tuple[date, float]]:
    """(date, value) rows of a fredgraph.csv download, oldest first. FRED writes "." for a missing value."""
    rows = []
    for line in text.strip().splitlines()[1:]:
        parts = line.strip().split(",")
        if len(parts) != 2 or parts[1].strip() in (".", ""):
            continue
        rows.append((date.fromisoformat(parts[0].strip()), float(parts[1])))
    if not rows:
        raise ValueError("no observations in FRED response")
    return sorted(rows)


def _on_or_before(rows: list[tuple[date, float]], day: date) -> tuple[date, float] | None:
    found = None
    for row in rows:
        if row[0] > day:
            break
        found = row
    return found


def summarize(series: Series, rows: list[tuple[date, float]]) -> dict:
    """Latest reading, its date, and its change over about a month (the previous print for monthly data)."""
    if series.kind == "yoy":
        yoy = []
        for day, value in rows:
            base = _on_or_before(rows, day - timedelta(days=365))
            if base is not None and (day - base[0]).days <= 380:
                yoy.append((day, (value / base[1] - 1) * 100))
        if not yoy:
            raise ValueError("less than a year of data for a year-on-year change")
        rows, unit = yoy, "% y/y"
    else:
        unit = ""
    day, value = rows[-1]
    out = {"series": series.id, "name": series.name + (f" ({unit})" if unit else ""), "currency": series.currency,
           "value": round(value, 2), "as_of": day.isoformat()}
    before = _on_or_before(rows, day - timedelta(days=28))
    if before is not None:
        out["change_1m"] = round(value - before[1], 2)
    return out


class FredData:
    """Downloads each configured series at most once a day and keeps the last good copy."""

    def __init__(self, series: list[Series], http: httpx.Client | None = None, refresh_hours: int = 24):
        self.series = series
        self.http = http or httpx.Client(timeout=20, follow_redirects=True,
                                         headers={"User-Agent": "Trade-Boy/0.2 (macro reader)"})
        self.refresh = timedelta(hours=refresh_hours)
        self.cached: dict[str, dict] = {}
        self.fetched_at: dict[str, datetime] = {}
        self.errors: dict[str, str] = {}

    def _refresh(self, wanted: list[Series], now: datetime) -> None:
        start = (now - timedelta(days=800)).date().isoformat()  # enough for a year-on-year change
        for s in wanted:
            last = self.fetched_at.get(s.id)
            if last is not None and now - last < self.refresh:
                continue
            try:
                resp = self.http.get(FRED_CSV, params={"id": s.id, "cosd": start})
                resp.raise_for_status()
                self.cached[s.id] = summarize(s, parse_csv(resp.text))
                self.errors.pop(s.id, None)
            except (httpx.HTTPError, ValueError) as exc:
                self.errors[s.id] = f"{s.name}: {str(exc)[:150] or type(exc).__name__}"
            self.fetched_at[s.id] = now

    def snapshot(self, currencies: set[str], now: datetime) -> list[dict]:
        """The latest readings for these currencies, plus the US 2s10s curve when both yields are there."""
        wanted = [s for s in self.series if s.currency in currencies]
        self._refresh(wanted, now)
        out = [self.cached[s.id] for s in wanted if s.id in self.cached]
        by_id = {row["series"]: row for row in out}
        if "DGS2" in by_id and "DGS10" in by_id:
            out.append({"series": "DGS10-DGS2", "name": "US 2s10s curve (10Y minus 2Y, pct points)", "currency": "USD",
                        "value": round(by_id["DGS10"]["value"] - by_id["DGS2"]["value"], 2),
                        "as_of": min(by_id["DGS10"]["as_of"], by_id["DGS2"]["as_of"])})
        return out

    def gaps(self, currencies: set[str]) -> list[str]:
        ids = {s.id for s in self.series if s.currency in currencies}
        return [f"FRED series unavailable, {err}" for sid, err in sorted(self.errors.items()) if sid in ids]
