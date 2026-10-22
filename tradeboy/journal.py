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
CREATE TABLE IF NOT EXISTS debates (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    trading_day TEXT NOT NULL,
    instrument TEXT NOT NULL,
    outcome TEXT NOT NULL,
    analysts_agreed_first INTEGER NOT NULL,
    analysts_agreed_last INTEGER NOT NULL,
    stance_changes INTEGER NOT NULL,
    transcript TEXT NOT NULL
);
-- Every directional call an analyst makes, scored once its time horizon has passed.
CREATE TABLE IF NOT EXISTS agent_calls (
    debate_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    instrument TEXT NOT NULL,
    stance TEXT NOT NULL,
    confidence INTEGER NOT NULL,
    price REAL NOT NULL,
    ts TEXT NOT NULL,
    due TEXT NOT NULL,
    result_pips REAL,
    PRIMARY KEY (debate_id, agent)
);
CREATE TABLE IF NOT EXISTS api_usage (
    ts TEXT NOT NULL,
    month TEXT NOT NULL,
    debate_id TEXT,
    agent TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    web_searches INTEGER NOT NULL,
    cost_usd REAL NOT NULL
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

    def get_state(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_state(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO state (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.db.commit()

    def is_halted(self) -> bool:
        return self.get_state("halted") == "1"

    def set_halted(self, halted: bool) -> None:
        self.set_state("halted", "1" if halted else "0")

    def record_debate(
        self, debate_id: str, ts: datetime, instrument: str, outcome: str,
        agreed_first: bool, agreed_last: bool, stance_changes: int, transcript: dict,
    ) -> None:
        self.db.execute(
            "INSERT INTO debates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (debate_id, ts.isoformat(), trading_day(ts), instrument, outcome, int(agreed_first),
             int(agreed_last), stance_changes, json.dumps(transcript)),
        )
        self.db.commit()

    def debates_today(self, ts: datetime) -> int:
        return self.db.execute("SELECT COUNT(*) FROM debates WHERE trading_day = ?", (trading_day(ts),)).fetchone()[0]

    def agreement_rate(self, last: int = 20) -> tuple[float, int] | None:
        """Share of recent debates where all analysts started on the same side, and how many debates."""
        rows = self.db.execute(
            "SELECT analysts_agreed_first FROM debates ORDER BY ts DESC LIMIT ?", (last,)
        ).fetchall()
        return (sum(r[0] for r in rows) / len(rows), len(rows)) if rows else None

    def record_call(
        self, debate_id: str, agent: str, instrument: str, stance: str, confidence: int,
        price: float, ts: datetime, due: datetime,
    ) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO agent_calls (debate_id, agent, instrument, stance, confidence, price, ts, due)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (debate_id, agent, instrument, stance, confidence, price, ts.isoformat(), due.isoformat()),
        )
        self.db.commit()

    def calls_due(self, ts: datetime) -> list[tuple[str, str, str, str, float, datetime]]:
        rows = self.db.execute(
            "SELECT debate_id, agent, instrument, stance, price, due FROM agent_calls"
            " WHERE result_pips IS NULL AND due <= ?", (ts.isoformat(),),
        ).fetchall()
        return [(*r[:5], datetime.fromisoformat(r[5])) for r in rows]

    def score_call(self, debate_id: str, agent: str, result_pips: float) -> None:
        self.db.execute(
            "UPDATE agent_calls SET result_pips = ? WHERE debate_id = ? AND agent = ?",
            (result_pips, debate_id, agent),
        )
        self.db.commit()

    def track_records(self, last: int = 50) -> dict[str, dict]:
        """Per agent: how many recent scored calls, share that were right, and average pips."""
        records = {}
        agents = [r[0] for r in self.db.execute("SELECT DISTINCT agent FROM agent_calls").fetchall()]
        for agent in agents:
            results = [r[0] for r in self.db.execute(
                "SELECT result_pips FROM agent_calls WHERE agent = ? AND result_pips IS NOT NULL"
                " ORDER BY ts DESC LIMIT ?", (agent, last),
            ).fetchall()]
            if results:
                records[agent] = {
                    "scored_calls": len(results),
                    "hit_rate_pct": round(100 * sum(r > 0 for r in results) / len(results)),
                    "avg_pips": round(sum(results) / len(results), 1),
                }
        return records

    def record_usage(self, ts: datetime, debate_id: str | None, usage: list) -> None:
        self.db.executemany(
            "INSERT INTO api_usage VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(ts.isoformat(), ts.strftime("%Y-%m"), debate_id, u.agent, u.model, u.input_tokens,
              u.output_tokens, u.web_searches, u.cost_usd) for u in usage],
        )
        self.db.commit()

    def month_spend(self, ts: datetime) -> float:
        row = self.db.execute("SELECT SUM(cost_usd) FROM api_usage WHERE month = ?", (ts.strftime("%Y-%m"),))
        return row.fetchone()[0] or 0.0

    def total_spend(self) -> float:
        """Everything ever spent on Claude, in USD."""
        return self.db.execute("SELECT SUM(cost_usd) FROM api_usage").fetchone()[0] or 0.0

    def day_spend(self, ts: datetime) -> float:
        start = (ts - timedelta(hours=24)).isoformat()
        row = self.db.execute("SELECT SUM(cost_usd) FROM api_usage WHERE ts > ?", (start,))
        return row.fetchone()[0] or 0.0

    def last_debate(self, instrument: str) -> datetime | None:
        row = self.db.execute("SELECT MAX(ts) FROM debates WHERE instrument = ?", (instrument,)).fetchone()
        return datetime.fromisoformat(row[0]) if row[0] else None

    def budget(self) -> tuple[float, float] | None:
        """(account NAV when the budget started, budget in account currency), or None if not started."""
        baseline, amount = self.get_state("baseline_nav"), self.get_state("budget_home")
        return (float(baseline), float(amount)) if baseline and amount else None

    def start_budget(self, baseline_nav: float, budget_home: float) -> None:
        """Begin a fresh budget: equity history restarts so old peaks don't count against it."""
        self.db.execute("DELETE FROM equity_marks")
        self.set_state("baseline_nav", repr(baseline_nav))
        self.set_state("budget_home", repr(budget_home))
