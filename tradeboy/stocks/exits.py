"""Looks after open positions every scan while the market is open. Exits come first.

- The stop: every position must have a live stop order at Alpaca. Fractional positions get day orders
  (Alpaca's rule), so their stop is placed again each session; a missing stop is replaced at once.
- Earnings: a position is closed the session before a scheduled report (a stop can't stop a gap).
- Time limit: closed once its trading days are up.
- Target: closed in code when price reaches it.
- Trailing stop (when the plan says so): once 1R in profit, the stop follows price 1R behind, never back.
- Re-review (one cheap agent call, hold or close, once per trigger): a new filing or news burst on the
  stock, or price halfway from entry to the stop.
- Positions that closed at Alpaca (stop filled) are recorded and reported.
R is the distance from entry to the stop set at entry.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from . import agents
from .executor import stop_order
from .market import ny_date, summarize
from .risk import trading_days_until

log = logging.getLogger("tradeboy")

AGAINST_TRIGGER_R = 0.5
TRAIL_START_R = 1.0


class StockTradeManager:
    def __init__(self, settings, broker, journal, llm, alerts, earnings=None, events=None, news=None,
                 dry_run: bool = True):
        self.settings = settings
        self.broker = broker
        self.journal = journal
        self.llm = llm
        self.alerts = alerts
        self.earnings = earnings
        self.events = events
        self.news = news
        self.dry_run = dry_run

    def manage(self, now: datetime, can_spend) -> None:
        mine = self.journal.open_trades()
        if not mine:
            return
        held = {p.symbol: p for p in self.broker.positions()}
        orders = self.broker.open_orders()
        quotes = self.broker.quotes(sorted({t["instrument"] for t in mine}))
        for trade in mine:
            symbol = trade["instrument"]
            if symbol not in held:
                self._closed(trade, now)
                continue
            try:
                self._check(trade, held[symbol], [o for o in orders if o["symbol"] == symbol], quotes.get(symbol),
                            now, can_spend)
            except Exception:  # one position's problem must not stop the others being looked after
                log.exception("Managing %s failed", symbol)

    def _check(self, trade, position, orders, quote, now, can_spend) -> None:
        symbol = trade["instrument"]
        long = trade["units"] > 0
        stop = self._ensure_stop(trade, position, orders)
        if quote is None:
            return
        price = quote.bid if long else quote.ask
        risk = abs(trade["entry_price"] - trade["stop_loss"])
        r_now = (price - trade["entry_price"]) * (1 if long else -1) / risk if risk else 0.0
        name = f"{symbol} {'long' if long else 'short'}"

        if self.settings.risk.close_before_earnings and self.earnings is not None:
            nxt = self.earnings.next_earnings(symbol, now)
            if nxt is not None and trading_days_until(ny_date(now), nxt) <= 1:
                self._close(trade, now, f"closed before earnings on {nxt} at {r_now:+.2f}R")
                return
        if trade["hold_until"] and now >= datetime.fromisoformat(trade["hold_until"]):
            self._close(trade, now, f"time limit reached at {r_now:+.2f}R")
            return
        target = trade["take_profit"]
        if target is not None and (price - target) * (1 if long else -1) >= 0:
            self._close(trade, now, f"target {target} reached at {r_now:+.2f}R")
            return
        if trade["exit_type"] == "trailing_stop" and r_now >= TRAIL_START_R and stop is not None:
            trail = round(price - risk if long else price + risk, 2)
            current = float(stop.get("stop_price") or 0)
            if (trail - current) * (1 if long else -1) >= 0.01:
                if not self.dry_run:
                    replaced = self.broker.replace_stop(stop["id"], trail)
                    self.journal.set_state(f"stop_order:{trade['trade_id']}", replaced.get("id", stop["id"]))
                self.journal.set_state(f"stop_level:{trade['trade_id']}", repr(trail))
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
            self.alerts.send(f"review:{trade['trade_id']}:{trigger}",
                             f"Re-review of {name} ({detail}): {answer['action'].upper()}. {answer['reason'][:300]}", now)
            if answer["action"] == "close":
                self._close(trade, now, f"closed on re-review ({detail}) at {r_now:+.2f}R")
                return

    def _ensure_stop(self, trade, position, orders) -> dict | None:
        """The position's live stop order, placing a new one when it is missing (or expired overnight)."""
        stops = [o for o in orders if o.get("type") in ("stop", "stop_limit")]
        if stops:
            return stops[0]
        # A trailing stop may have moved: keep the best level recorded, never the looser entry stop.
        level = float(self.journal.get_state(f"stop_level:{trade['trade_id']}") or trade["stop_loss"])
        if self.dry_run:
            return None
        order = stop_order(self.broker, trade["instrument"], position.qty, level)
        self.journal.set_state(f"stop_order:{trade['trade_id']}", order["id"])
        log.info("Placed the stop for %s at %s.", trade["instrument"], level)
        return order

    def _triggers(self, trade, r_now: float, now: datetime) -> list[tuple[str, str]]:
        found = []
        if r_now <= -AGAINST_TRIGGER_R:
            found.append(("against", f"price {r_now:+.2f}R, halfway to the stop"))
        if self.events is not None:
            for e in self.events.related(trade["instrument"], now):
                found.append((e.key, e.summary))
        return found

    def _review(self, trade, trigger, price, r_now, now) -> dict | None:
        symbol = trade["instrument"]
        context = {
            "position": {"symbol": symbol, "side": "long" if trade["units"] > 0 else "short", "entry": trade["entry_price"],
                         "stop_loss": trade["stop_loss"], "take_profit": trade["take_profit"], "opened_at": trade["opened_at"],
                         "time_limit": trade["hold_until"], "price_now": price, "result_in_R": round(r_now, 2)},
            "head_of_desk_reasoning": (trade.get("rationale") or "")[:1500],
            "exit_plan": {"type": trade["exit_type"], "notes": trade["exit_reason"]},
            "trigger": trigger,
            "daily": summarize(self.broker.bars(symbol, "1Day", 60)),
            "hourly": summarize(self.broker.bars(symbol, "1Hour", 60)),
        }
        if self.news is not None:
            context["headlines"] = [{"time": h.time.isoformat(), "title": h.title}
                                    for h in self.news.headlines({symbol}, now, hours=24, limit=10)]
        try:
            return self.llm.ask(
                agent="trade_reviewer", model=self.settings.claude.analyst_model, system=agents.TRADE_REVIEW,
                prompt=f"Time (UTC): {now.isoformat(timespec='minutes')}\n{json.dumps(context, indent=1, default=str)}",
                schema=agents.REVIEW_SCHEMA, effort="low",
            )
        except Exception as exc:
            log.error("Re-review of %s failed: %s", symbol, exc)
            return None

    def _close(self, trade, now, why: str) -> None:
        log.info("Closing %s: %s", trade["instrument"], why)
        self.journal.set_close_note(trade["trade_id"], why)
        if self.dry_run:
            return
        self.broker.close_position(trade["instrument"])
        self.journal.set_state(f"closing:{trade['trade_id']}", now.isoformat())

    def _closed(self, trade, now) -> None:
        """The position is gone at Alpaca: record the result from the closing fill."""
        exit_price = self._exit_price(trade)
        pl = None
        if exit_price is not None:
            pl = round((exit_price - trade["entry_price"]) * trade["units"], 2)
        note = self.journal.mark_closed(trade["trade_id"], now, pl) or "stop loss filled at Alpaca"
        side = "long" if trade["units"] > 0 else "short"
        self.alerts.send(f"closed:{trade['trade_id']}",
                         f"Closed {trade['instrument']} {side}: {note}. Result "
                         f"{'unknown' if pl is None else f'{pl:+.2f} USD'}.", now)

    def _exit_price(self, trade) -> float | None:
        """The average price of the latest filled order that reduced this position."""
        try:
            orders = self.broker.closed_orders(trade["instrument"], after=trade["opened_at"])
        except Exception as exc:
            log.error("Could not read %s's closing order: %s", trade["instrument"], exc)
            return None
        exit_side = "sell" if trade["units"] > 0 else "buy"
        fills = [o for o in orders if o.get("side") == exit_side and o.get("status") == "filled" and o["id"] != trade["trade_id"]]
        if not fills:
            return None
        latest = max(fills, key=lambda o: o.get("filled_at") or "")
        return float(latest["filled_avg_price"])
