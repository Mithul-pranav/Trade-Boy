"""Scripted stand-ins for Claude and OANDA, so the desk can be tested without either."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from tradeboy.llm import Usage
from tradeboy.models import AccountState, Candle, Quote


def view(stance="long", confidence=60, thesis="", hold_hours=24, **extra):
    return {"stance": stance, "confidence": confidence, "thesis": thesis or f"{stance} thesis",
            "evidence": ["e"], "invalidation": "x", "hold_hours": hold_hours, **extra}


def technical(stance="long", stop=1.0950, target=1.1100, **kw):
    return view(stance, stop_loss=stop, take_profit=target, **kw)


def rebuttal(stance="long", changed_by="", confidence=55):
    return {"strongest_counterargument": {"agent": "macro", "argument": "a"}, "reply": "r",
            "final_stance": stance, "final_confidence": confidence, "changed_mind": bool(changed_by),
            "changed_by": changed_by}


def skeptic(*objections):
    return {"objections": [
        {"id": oid, "severity": sev, "claim": "c", "evidence": "e"} for oid, sev in objections
    ], "best_case_against": "b"}


def risk(long=1.0, short=1.0):
    return {"long_size": long, "short_size": short, "concerns": [], "summary": "s"}


def head(side="long", stop=1.0950, target=1.1100, answers=(), size=1.0):
    return {"side": side, "size": size, "stop_loss": stop, "take_profit": target, "hold_hours": 24,
            "objection_answers": [{"objection_id": o, "answer": "a", "resolved": r} for o, r in answers],
            "dissent": "d", "reasoning": "because"}


def agreeing_script():
    return {
        "technical": technical(), "macro": view(), "positioning": view(),
        "skeptic": skeptic(("O1", "serious")), "risk_manager": risk(0.5, 1.0),
        "head_of_desk": head(answers=[("O1", True)]),
    }


class FakeLLM:
    def __init__(self, script: dict, cost_per_call: float = 0.01):
        self.script = script
        self.calls: list[dict] = []
        self.cost_per_call = cost_per_call
        self.usage: list[Usage] = []
        self.budget_left_usd = float("inf")
        self.lock = threading.Lock()

    def ask(self, *, agent, model, system, prompt, schema, effort="medium", web_searches=0):
        with self.lock:
            self.calls.append({"agent": agent, "model": model, "prompt": prompt, "web_searches": web_searches})
            self.usage.append(Usage(agent, model, 100, 50, 0, self.cost_per_call))
        answer = self.script[agent]
        return answer() if callable(answer) else dict(answer)

    def drain_usage(self):
        usage, self.usage = self.usage, []
        return usage

    def agents_called(self):
        return [c["agent"] for c in self.calls]


def candles(start=1.1000, step=0.0002, n=100, granularity_minutes=60):
    t0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
    out, price = [], start
    for i in range(n):
        out.append(Candle(t0 + timedelta(minutes=granularity_minutes * i), price, price + 0.0010,
                          price - 0.0010, price + step, 1000))
        price += step
    return out


class FakeBroker:
    def __init__(self, nav=100_000.0, trades=(), later_price=None):
        self.nav = nav
        self.trades = list(trades)
        self.orders = []
        self.later_price = later_price
        self.candle_calls = 0

    def account(self):
        return AccountState(self.nav, self.nav, "SGD", list(self.trades))

    def quotes(self, instruments):
        return {i: Quote(i, 1.1000, 1.1001, 1.35) for i in instruments}

    def to_home_rate(self, currency, via_instrument):
        return 1.30

    def candles(self, instrument, granularity="M15", count=200):
        self.candle_calls += 1
        return candles(n=count)

    def position_book(self, instrument):
        return {"retail_long_pct": 70.0, "retail_short_pct": 30.0}

    def price_at(self, instrument, when):
        return self.later_price

    def market_order(self, instrument, units, stop_loss, take_profit=None):
        self.orders.append((instrument, units, stop_loss, take_profit))
        return {"orderFillTransaction": {"price": "1.1001", "tradeOpened": {"tradeID": "42"}}}

    def close_trade(self, trade_id):
        return {}
