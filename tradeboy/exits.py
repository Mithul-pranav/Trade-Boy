"""Looks after open trades every scan: the exit plan written at entry, checked in code, with an agent
re-review only when something has changed.

- Time limit: every trade is closed once its hold_hours have passed.
- Trailing stop (when the plan says so): once a trade is 1R in profit, its stop at OANDA follows price 1R behind.
- Triggers for a re-review (one cheap agent call, hold or close, once per trigger per trade): a
  high-impact event for either currency within the hour, or price halfway from entry to the stop.
- Trades that closed at OANDA (stop or target hit) are recorded and reported.

R is the distance from entry to the stop set at entry.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from . import agents
from .market import summarize
from .models import currencies, pip_size

log = logging.getLogger("tradeboy")

EVENT_WINDOW = timedelta(hours=1)
AGAINST_TRIGGER_R = 0.5  # share of the way from entry to the stop that asks for a re-review
TRAIL_START_R = 1.0


class TradeManager:
    def __init__(self, settings, broker, journal, llm, alerts, calendar=None, news=None, dry_run: bool = True):
        self.settings = settings
        self.broker = broker
        self.journal = journal
        self.llm = llm
        self.alerts = alerts
        self.calendar = calendar
        self.news = news
        self.dry_run = dry_run

    def manage(self, now: datetime, can_spend) -> None:
        """One pass over the desk's open trades. can_spend() says whether an agent call is affordable now."""
        mine = self.journal.open_trades()
        if not mine:
            return
        at_broker = {t.trade_id: t for t in self.broker.open_trades()}
        quotes = self.broker.quotes(sorted({t["instrument"] for t in mine}))
        for trade in mine:
            if trade["trade_id"] not in at_broker:
                self._closed(trade, now)
                continue
            try:
                self._check(trade, at_broker[trade["trade_id"]], quotes[trade["instrument"]], now, can_spend)
            except Exception:  # one trade's problem must not stop the others being looked after
                log.exception("Managing trade %s failed", trade["trade_id"])

    # -- the checks ------------------------------------------------------------------------------

    def _check(self, trade: dict, live, quote, now: datetime, can_spend) -> None:
        long = trade["units"] > 0
        price = quote.bid if long else quote.ask  # the price the trade would close at
        risk = abs(trade["entry_price"] - trade["stop_loss"])
        r_now = (price - trade["entry_price"]) * (1 if long else -1) / risk if risk else 0.0
        name = f"{trade['instrument']} {'long' if long else 'short'} (trade {trade['trade_id']})"

        if trade["hold_until"] and now >= datetime.fromisoformat(trade["hold_until"]):
            self._close(trade, now, f"time limit reached at {r_now:+.2f}R")
            return

        if trade["exit_type"] == "trailing_stop" and r_now >= TRAIL_START_R:
            trail = price - risk if long else price + risk
            current = live.stop_loss
            if current is None or (trail - current) * (1 if long else -1) >= pip_size(trade["instrument"]):
                digits = 3 if trade["instrument"].endswith("_JPY") else 5
                trail = round(trail, digits)
                if not self.dry_run:
                    self.broker.set_stop(trade["trade_id"], trade["instrument"], trail)
                log.info("Trailing stop on %s moved to %s (%+.2fR).", name, trail, r_now)

        for trigger, detail in self._triggers(trade, r_now, now):
            if trigger in trade["reviewed"]:
                continue
            if not can_spend():
                log.info("Re-review of %s for %s skipped: no API budget left now.", name, detail)
                return
            self.journal.mark_reviewed(trade["trade_id"], trigger)
            answer = self._review(trade, detail, price, r_now, now)
            if answer is None:
                continue
            self.alerts.send(
                f"review:{trade['trade_id']}:{trigger}",
                f"Re-review of {name} ({detail}): {answer['action'].upper()}. {answer['reason'][:300]}", now,
            )
            if answer["action"] == "close":
                self._close(trade, now, f"closed on re-review ({detail}) at {r_now:+.2f}R")
                return

    def _triggers(self, trade: dict, r_now: float, now: datetime) -> list[tuple[str, str]]:
        found = []
        if r_now <= -AGAINST_TRIGGER_R:
            found.append(("against", f"price {r_now:+.2f}R, halfway to the stop"))
        if self.calendar is not None:
            for event in self.calendar.upcoming(set(currencies(trade["instrument"])), now, hours=1):
                if event.impact == "high" and now <= event.time <= now + EVENT_WINDOW:
                    found.append((f"event:{event.currency}:{event.title}:{event.time.isoformat()}",
                                  f"{event.currency} {event.title} at {event.time:%H:%M} UTC"))
        return found

    def _review(self, trade: dict, trigger: str, price: float, r_now: float, now: datetime) -> dict | None:
        instrument = trade["instrument"]
        context = {
            "trade": {
                "instrument": instrument, "side": "long" if trade["units"] > 0 else "short",
                "entry": trade["entry_price"], "stop_loss": trade["stop_loss"], "take_profit": trade["take_profit"],
                "opened_at": trade["opened_at"], "time_limit": trade["hold_until"], "price_now": price,
                "result_in_R": round(r_now, 2),
            },
            "head_of_desk_reasoning": (trade.get("rationale") or "")[:1500],
            "exit_plan": {"type": trade["exit_type"], "notes": trade["exit_reason"]},
            "trigger": trigger,
            "H1": summarize(instrument, self.broker.candles(instrument, "H1", 100)),
            "H4": summarize(instrument, self.broker.candles(instrument, "H4", 100)),
        }
        mine = set(currencies(instrument))
        if self.calendar is not None:
            context["events"] = [
                {"time": e.time.isoformat(), "currency": e.currency, "title": e.title, "impact": e.impact}
                for e in self.calendar.upcoming(mine, now, hours=24)
            ]
        if self.news is not None:
            context["headlines"] = [
                {"time": h.time.isoformat(), "source": h.source, "title": h.title}
                for h in self.news.headlines(mine, now, hours=24, limit=10)
            ]
        try:
            return self.llm.ask(
                agent="trade_reviewer", model=self.settings.claude.analyst_model, system=agents.TRADE_REVIEW,
                prompt=f"Time (UTC): {now.isoformat(timespec='minutes')}\n{json.dumps(context, indent=1, default=str)}",
                schema=agents.REVIEW_SCHEMA, effort="low",
            )
        except Exception as exc:
            log.error("Re-review of trade %s failed: %s", trade["trade_id"], exc)
            return None

    # -- closing ---------------------------------------------------------------------------------

    def _close(self, trade: dict, now: datetime, why: str) -> None:
        log.info("Closing trade %s: %s", trade["trade_id"], why)
        self.journal.set_close_note(trade["trade_id"], why)
        if not self.dry_run:
            self.broker.close_trade(trade["trade_id"])
        self._closed(trade, now)

    def _closed(self, trade: dict, now: datetime) -> None:
        details = self.broker.trade_details(trade["trade_id"])
        if details["state"] == "OPEN":
            return
        note = self.journal.mark_closed(trade["trade_id"], now, details["realized_pl"]) or "stop loss or target hit at OANDA"
        side = "long" if trade["units"] > 0 else "short"
        self.alerts.send(
            f"closed:{trade['trade_id']}",
            f"Closed {trade['instrument']} {side} (trade {trade['trade_id']}): {note}. "
            f"Result {details['realized_pl']:+.2f} in the account currency.",
            now,
        )
