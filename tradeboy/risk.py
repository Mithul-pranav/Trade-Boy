"""Hard risk limits. Plain code that every order passes through; no agent can argue past it."""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from .config import RiskLimits
from .models import (
    AccountState,
    CalendarEvent,
    Quote,
    RiskDecision,
    Side,
    TradeProposal,
    currencies,
)


def drawdown_pct(nav: float, peak_nav: float) -> float:
    if peak_nav <= 0:
        return 0.0
    return max(0.0, (peak_nav - nav) / peak_nav * 100)


def daily_loss_pct(nav: float, day_start_nav: float) -> float:
    if day_start_nav <= 0:
        return 0.0
    return max(0.0, (day_start_nav - nav) / day_start_nav * 100)


def kill_switch_tripped(limits: RiskLimits, nav: float, peak_nav: float) -> bool:
    return drawdown_pct(nav, peak_nav) >= limits.max_drawdown_pct


def position_units(risk_amount: float, entry: float, stop: float, quote_to_home_loss: float) -> int:
    """Units such that hitting the stop loses at most risk_amount in the account currency."""
    loss_per_unit = abs(entry - stop) * quote_to_home_loss
    if loss_per_unit <= 0:
        return 0
    return math.floor(risk_amount / loss_per_unit)


def in_news_blackout(
    instrument: str, events: list[CalendarEvent], now: datetime, minutes: int
) -> CalendarEvent | None:
    window = timedelta(minutes=minutes)
    pair = set(currencies(instrument))
    for event in events:
        if event.impact == "high" and event.currency in pair and abs(event.time - now) <= window:
            return event
    return None


def evaluate(
    limits: RiskLimits,
    proposal: TradeProposal,
    account: AccountState,
    quote: Quote,
    *,
    peak_nav: float,
    day_start_nav: float,
    halted: bool,
    events: list[CalendarEvent],
    now: datetime,
) -> RiskDecision:
    """Check a proposal against every hard limit and size it. Rejections list every reason."""
    if halted:
        return RiskDecision(False, reasons=["Desk is halted by the kill switch; a human must resume it."])
    if kill_switch_tripped(limits, account.nav, peak_nav):
        dd = drawdown_pct(account.nav, peak_nav)
        return RiskDecision(
            False,
            reasons=[f"Drawdown {dd:.2f}% hit the {limits.max_drawdown_pct}% kill switch."],
            kill_switch=True,
        )

    reasons: list[str] = []

    loss = daily_loss_pct(account.nav, day_start_nav)
    if loss >= limits.daily_loss_limit_pct:
        reasons.append(f"Daily loss {loss:.2f}% reached the {limits.daily_loss_limit_pct}% limit.")

    entry = quote.ask if proposal.side is Side.BUY else quote.bid
    if proposal.side is Side.BUY and proposal.stop_loss >= entry:
        reasons.append("Stop loss must be below the entry price on a buy.")
    if proposal.side is Side.SELL and proposal.stop_loss <= entry:
        reasons.append("Stop loss must be above the entry price on a sell.")
    if proposal.take_profit is not None:
        if proposal.side is Side.BUY and proposal.take_profit <= entry:
            reasons.append("Take profit must be above the entry price on a buy.")
        if proposal.side is Side.SELL and proposal.take_profit >= entry:
            reasons.append("Take profit must be below the entry price on a sell.")

    if len(account.open_trades) >= limits.max_open_trades:
        reasons.append(f"Already {len(account.open_trades)} open trades (max {limits.max_open_trades}).")
    if any(t.instrument == proposal.instrument for t in account.open_trades):
        reasons.append(f"A {proposal.instrument} position is already open (netting account).")

    if quote.spread_pips > limits.max_spread_pips:
        reasons.append(f"Spread {quote.spread_pips:.1f} pips is wider than {limits.max_spread_pips}.")

    event = in_news_blackout(proposal.instrument, events, now, limits.news_blackout_minutes)
    if event:
        reasons.append(f"High-impact news within {limits.news_blackout_minutes} min: {event.currency} {event.title}.")

    risk_budget = account.nav * limits.risk_per_trade_pct / 100
    units = position_units(risk_budget, entry, proposal.stop_loss, quote.quote_to_home_loss)
    risk_amount = units * abs(entry - proposal.stop_loss) * quote.quote_to_home_loss
    if units < 1:
        reasons.append("Position size rounds to zero units.")

    open_risk = sum(t.risk_amount for t in account.open_trades)
    max_total = account.nav * limits.max_total_risk_pct / 100
    if open_risk + risk_amount > max_total:
        reasons.append(
            f"Total risk would be {open_risk + risk_amount:,.2f} {account.currency}, "
            f"over the {limits.max_total_risk_pct}% cap ({max_total:,.2f})."
        )

    if reasons:
        return RiskDecision(False, reasons=reasons)
    signed = units if proposal.side is Side.BUY else -units
    return RiskDecision(True, units=signed, risk_amount=round(risk_amount, 2))
