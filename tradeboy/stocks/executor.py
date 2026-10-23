"""Turns the head of desk's call into orders at Alpaca, but only after the hard limits pass.

Entry is a market order. As soon as it fills, a stop order for the filled shares goes in: good till
cancelled for whole shares, a day order for fractional ones (Alpaca's rule), which the exit manager
places again every session. Targets and time limits are watched in code.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..journal import Journal
from ..models import Side
from ..risk import kill_switch_reason
from .config import StockSettings
from .market import atr_pct
from .models import StockDecision, StockProposal
from .risk import evaluate
from .team import slippage_bps

log = logging.getLogger("tradeboy")


@dataclass
class StockExecution:
    decision: StockDecision
    decision_id: int
    trade_id: str | None = None
    fill_price: float | None = None
    filled_qty: float = 0.0


def stop_order(broker, symbol: str, qty: float, stop: float) -> dict:
    """The protective stop for a position of qty shares (negative for a short)."""
    whole = abs(qty) == int(abs(qty))
    return broker.submit_order(symbol, abs(qty), "sell" if qty > 0 else "buy", "stop", stop_price=stop,
                               time_in_force="gtc" if whole else "day")


class StockExecutor:
    def __init__(self, settings: StockSettings, broker, journal: Journal, earnings=None, dry_run: bool = True):
        self.settings = settings
        self.broker = broker
        self.journal = journal
        self.earnings = earnings
        self.dry_run = dry_run

    # -- the budget ----------------------------------------------------------------------------

    def equity(self) -> float:
        """The desk's budget equity: the budget plus everything the account has made or lost since it started."""
        account_equity = self.broker.account()["equity"]
        budget = self.journal.budget()
        if budget is None:
            self.journal.start_budget(account_equity, self.settings.starting_capital)
            budget = (account_equity, self.settings.starting_capital)
        baseline, amount = budget
        return amount + account_equity - baseline

    def open_positions(self) -> list[dict]:
        """Positions at Alpaca with the risk the desk recorded for them (unknown ones count at full risk)."""
        mine = {t["instrument"]: t for t in self.journal.open_trades()}
        per_trade = max(self.equity(), 0) * self.settings.risk.risk_per_trade_pct / 100
        out = []
        for p in self.broker.positions():
            trade = mine.get(p.symbol)
            risk = self.journal.risk_for(trade["trade_id"]) if trade else None
            out.append({"symbol": p.symbol, "qty": p.qty, "value": abs(p.qty) * p.current_price,
                        "risk": risk if risk is not None else per_trade, "trade_id": trade["trade_id"] if trade else None})
        return out

    def check_kill_switch(self, now: datetime | None = None) -> bool:
        """Record equity and, if the original budget is gone, close everything and halt."""
        now = now or datetime.now(timezone.utc)
        equity = self.equity()
        self.journal.mark_equity(now, equity)
        if self.journal.is_halted():
            return True
        if not kill_switch_reason(self.settings.risk, equity, self.journal.peak_nav() or equity):
            return False
        self.journal.set_halted(True)
        if not self.dry_run:
            for p in self.broker.positions():
                self.broker.close_position(p.symbol)
        return True

    # -- one order -----------------------------------------------------------------------------

    def execute(self, proposal: StockProposal, now: datetime | None = None, max_share: float = 1.0,
                daily_bars=None) -> StockExecution:
        now = now or datetime.now(timezone.utc)
        equity = self.equity()
        self.journal.mark_equity(now, equity)
        quote = self.broker.quotes([proposal.symbol])[proposal.symbol]
        next_earnings = self.earnings.next_earnings(proposal.symbol, now) if self.earnings else None
        decision = evaluate(
            self.settings.risk, proposal, nav=equity, open_positions=self.open_positions(), quote=quote,
            asset=self.broker.asset(proposal.symbol), clock=self.broker.clock(),
            peak_nav=self.journal.peak_nav() or equity, day_start_nav=self.journal.day_start_nav(now) or equity,
            halted=self.journal.is_halted(), next_earnings=next_earnings, allow_shorts=self.settings.allow_shorts,
            max_share=max_share,
        )
        decision_id = self.journal.record_decision(
            now, proposal.symbol, proposal.side.value, decision.approved, decision.qty, decision.risk_amount,
            decision.reasons, proposal.rationale, proposal.debate_id, self.dry_run,
        )
        if decision.kill_switch:
            self.check_kill_switch(now)
        if not decision.approved or self.dry_run:
            return StockExecution(decision, decision_id)

        side = "buy" if proposal.side is Side.BUY else "sell"
        order = self.broker.submit_order(proposal.symbol, abs(decision.qty), side, "market", time_in_force="day")
        order = self.broker.wait_for_fill(order["id"])
        filled = float(order.get("filled_qty") or 0)
        if filled <= 0:
            if order["status"] not in ("canceled", "expired", "rejected"):
                self.broker.cancel_order(order["id"])
            decision.reasons.append(f"Entry order not filled (status {order['status']}).")
            return StockExecution(decision, decision_id)
        qty = filled if decision.qty > 0 else -filled
        price = float(order["filled_avg_price"])
        stop = stop_order(self.broker, proposal.symbol, qty, proposal.stop_loss)
        risk_amount = round(abs(price - proposal.stop_loss) * filled, 2)
        trade_id = order["id"]
        self.journal.record_trade(
            trade_id, decision_id, proposal.symbol, qty, price, proposal.stop_loss, proposal.take_profit,
            risk_amount, now, hold_hours=hours_for(now, proposal.hold_days), exit_type=proposal.exit_type,
            exit_reason=proposal.exit_reason,
        )
        self.journal.set_state(f"stop_order:{trade_id}", stop["id"])
        self.journal.set_state(f"execution:{now.isoformat()}:{proposal.symbol}", json.dumps({
            "symbol": proposal.symbol, "side": side, "decision_mid": quote.mid, "fill": price,
            "slippage_bps": slippage_bps(side, quote.mid, price),
            "stop_vs_atr": round(abs(price - proposal.stop_loss) / price * 100 / atr_pct(daily_bars), 2)
            if daily_bars and atr_pct(daily_bars) else None,
        }))
        return StockExecution(decision, decision_id, trade_id, price, qty)


def hours_for(now: datetime, trading_days: int) -> int:
    """Hours from now to the same time of day this many weekdays later (the position's time limit)."""
    end, left = now, max(1, trading_days)
    while left:
        end += timedelta(days=1)
        if end.weekday() < 5:
            left -= 1
    return round((end - now).total_seconds() / 3600)
