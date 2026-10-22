"""SQLite journal: every decision, order and equity mark, so any trade can be explained later."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    instrument TEXT NOT NULL,
    side TEXT NOT NULL,
    approved INTEGER NOT NULL,
    units INTEGER NOT NULL,
    risk_amount REAL NOT NULL,
    reasons TEXT NOT NULL,
    rationale TEXT NOT NULL,
    debate_id TEXT,
    dry_run INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
    trade_id TEXT PRIMARY KEY,
    decision_id INTEGER REFERENCES decisions(id),
    instrument TEXT NOT NULL,
    units INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    stop_loss REAL NOT NULL,
    take_profit REAL,
    risk_amount REAL NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    realized_pl REAL
);
CREATE TABLE IF NOT EXISTS equity_marks (
    ts TEXT NOT NULL,
    trading_day TEXT NOT NULL,
    nav REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def trading_day(ts: datetime) -> str:
    """Forex days roll over at 17:00 New York time."""
    return (ts.astimezone(NEW_YORK) + timedelta(hours=7)).date().isoformat()


class Journal:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.executescript(SCHEMA)

    def record_decision(
        self, ts: datetime, instrument: str, side: str, approved: bool, units: int,
        risk_amount: float, reasons: list[str], rationale: str, debate_id: str | None, dry_run: bool,
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO decisions (ts, instrument, side, approved, units, risk_amount, reasons,"
            " rationale, debate_id, dry_run) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts.isoformat(), instrument, side, int(approved), units, risk_amount,
             json.dumps(reasons), rationale, debate_id, int(dry_run)),
        )
        self.db.commit()
        return cur.lastrowid

    def record_trade(
        self, trade_id: str, decision_id: int, instrument: str, units: int, entry_price: float,
        stop_loss: float, take_profit: float | None, risk_amount: float, opened_at: datetime,
    ) -> None:
        self.db.execute(
            "INSERT INTO trades (trade_id, decision_id, instrument, units, entry_price, stop_loss,"
            " take_profit, risk_amount, opened_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (trade_id, decision_id, instrument, units, entry_price, stop_loss, take_profit,
             risk_amount, opened_at.isoformat()),
        )
        self.db.commit()

    def risk_for(self, trade_id: str) -> float | None:
        row = self.db.execute("SELECT risk_amount FROM trades WHERE trade_id = ?", (trade_id,)).fetchone()
        return row[0] if row else None

    def mark_equity(self, ts: datetime, nav: float) -> None:
        self.db.execute(
            "INSERT INTO equity_marks (ts, trading_day, nav) VALUES (?, ?, ?)",
            (ts.isoformat(), trading_day(ts), nav),
        )
        self.db.commit()

    def peak_nav(self) -> float | None:
        return self.db.execute("SELECT MAX(nav) FROM equity_marks").fetchone()[0]

    def day_start_nav(self, ts: datetime) -> float | None:
        row = self.db.execute(
            "SELECT nav FROM equity_marks WHERE trading_day = ? ORDER BY ts LIMIT 1",
            (trading_day(ts),),
        ).fetchone()
        return row[0] if row else None

    def is_halted(self) -> bool:
        row = self.db.execute("SELECT value FROM state WHERE key = 'halted'").fetchone()
        return bool(row and row[0] == "1")

    def set_halted(self, halted: bool) -> None:
        self.db.execute(
            "INSERT INTO state (key, value) VALUES ('halted', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("1" if halted else "0",),
        )
        self.db.commit()
