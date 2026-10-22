"""Years of OANDA candles with bid and ask prices, downloaded once and kept on disk for backtests.

Each row keeps the mid prices (what the desk's indicators use) and the bid and ask prices (what a trade
actually pays), so a backtest charges the real historical spread instead of a guess.
"""

from __future__ import annotations

import bisect
import csv
import gzip
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import ROOT
from .oanda import _parse_time

HISTORY_DIR = ROOT / "data" / "history"
DURATION = {"H1": timedelta(hours=1), "H4": timedelta(hours=4), "D": timedelta(days=1)}
FIELDS = ["time", "volume", "o", "h", "l", "c", "bo", "bh", "bl", "bc", "ao", "ah", "al", "ac"]
PAGE = 5000  # OANDA's most candles per request


@dataclass(frozen=True)
class Bar:
    """One candle. open/high/low/close are mid prices, so the desk's indicator code reads it like a Candle."""

    time: datetime  # when the candle opened
    open: float
    high: float
    low: float
    close: float
    volume: int
    bid_open: float
    bid_high: float
    bid_low: float
    bid_close: float
    ask_open: float
    ask_high: float
    ask_low: float
    ask_close: float


def path_for(instrument: str, granularity: str, folder: Path = HISTORY_DIR) -> Path:
    return folder / f"{instrument}_{granularity}.csv.gz"


def _row(c: dict) -> list:
    return [c["time"], c["volume"], *(c[k][f] for k in ("mid", "bid", "ask") for f in "ohlc")]


def download(client, instrument: str, granularity: str, start: datetime, end: datetime | None = None,
             folder: Path = HISTORY_DIR) -> Path:
    """Fetch complete candles from `start` (or from where the saved file ends) up to `end`, and save them."""
    path = path_for(instrument, granularity, folder)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    if path.exists():
        with gzip.open(path, "rt", newline="") as f:
            rows = list(csv.reader(f))[1:]
        if rows:
            start = _parse_time(rows[-1][0]) + DURATION[granularity]
    end = end or datetime.now(timezone.utc)
    while start < end:
        data = client._request(
            "GET", f"/v3/instruments/{instrument}/candles",
            params={"granularity": granularity, "price": "MBA", "from": start.isoformat(), "count": PAGE},
        )
        page = [c for c in data["candles"] if c.get("complete") and _parse_time(c["time"]) < end]
        if not page:
            break
        rows.extend(_row(c) for c in page)
        start = _parse_time(page[-1]["time"]) + DURATION[granularity]
        if len(data["candles"]) < PAGE:
            break
    with gzip.open(path, "wt", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(FIELDS)
        writer.writerows(rows)
    return path


def load(instrument: str, granularity: str, folder: Path = HISTORY_DIR) -> list[Bar]:
    path = path_for(instrument, granularity, folder)
    if not path.exists():
        raise FileNotFoundError(f"No saved {granularity} history for {instrument}; run `tradeboy history` first.")
    with gzip.open(path, "rt", newline="") as f:
        reader = csv.reader(f)
        next(reader)
        return [
            Bar(_parse_time(r[0]), *map(float, r[2:6]), int(r[1]), *map(float, r[6:14]))
            for r in reader
        ]


# -- short-term interest rates, for overnight financing and the carry signal ---------------------------------

# FRED's monthly 3-month interbank rates (OECD), one per currency. Monthly averages are published after
# the month ends, so a backtest only uses a month's rate from the start of the month after next.
RATE_SERIES = {
    "USD": "IR3TIB01USM156N", "EUR": "IR3TIB01EZM156N", "GBP": "IR3TIB01GBM156N", "JPY": "IR3TIB01JPM156N",
    "AUD": "IR3TIB01AUM156N", "CAD": "IR3TIB01CAM156N", "CHF": "IR3TIB01CHM156N", "NZD": "IR3TIB01NZM156N",
}


def rates_path(folder: Path = HISTORY_DIR) -> Path:
    return folder / "short_rates.csv"


def download_rates(http=None, folder: Path = HISTORY_DIR) -> Path:
    import httpx

    from .macro import FRED_CSV, parse_csv

    http = http or httpx.Client(timeout=30, follow_redirects=True)
    path = rates_path(folder)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["currency", "month", "rate_pct"])
        for currency, series in RATE_SERIES.items():
            resp = http.get(FRED_CSV, params={"id": series})
            resp.raise_for_status()
            for day, value in parse_csv(resp.text):
                writer.writerow([currency, day.isoformat(), value])
    return path


class ShortRates:
    """Each currency's 3-month rate as it was known at a given time (the latest published month)."""

    def __init__(self, rows: dict[str, list[tuple[datetime, float]]]):
        self.rows = {c: sorted(r) for c, r in rows.items()}

    @classmethod
    def load(cls, folder: Path = HISTORY_DIR) -> "ShortRates | None":
        path = rates_path(folder)
        if not path.exists():
            return None
        rows: dict[str, list] = {}
        with path.open(newline="") as f:
            for r in csv.DictReader(f):
                month = datetime.fromisoformat(r["month"]).replace(tzinfo=timezone.utc)
                y, m = divmod(month.month + 1, 12)  # usable from the first day of the month after next
                known = month.replace(year=month.year + y, month=m + 1)
                rows.setdefault(r["currency"], []).append((known, float(r["rate_pct"])))
        return cls(rows)

    def rate(self, currency: str, when: datetime) -> float | None:
        rows = self.rows.get(currency, [])
        k = bisect.bisect_right(rows, (when, float("inf")))
        return rows[k - 1][1] if k else None

    def differential(self, instrument: str, when: datetime) -> float | None:
        """Base currency's rate minus the quote currency's, in percentage points: what a long earns."""
        base, quote = instrument.split("_")
        rb, rq = self.rate(base, when), self.rate(quote, when)
        return None if rb is None or rq is None else round(rb - rq, 3)
