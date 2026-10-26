"""Hard limits for the stocks desk. Plain code every order passes through; no agent can argue past it."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

from ..models import Side
from ..risk import daily_loss_pct, kill_switch_reason
from .config import StockLimits
from .models import Asset, Clock, StockDecision, StockProposal, StockQuote

FRACTION_DIGITS = 4  # Alpaca takes up to 9; 4 keeps a 1000 USD budget's positions exact enough


def trading_days_until(today: date, target: date) -> int:
    """Weekdays from today to target (holidays ignored, which errs on the cautious side)."""
    days, d = 0, today
    while d < target:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days


def size(risk_budget: float, entry: float, stop: float, max_value: float, fractional: bool) -> float:
    """Shares such that the stop loses at most risk_budget and the position is worth at most max_value."""
    per_share = abs(entry - stop)
    if per_share <= 0 or entry <= 0:
        return 0.0
    qty = min(risk_budget / per_share, max_value / entry)
    if fractional:
        return math.floor(qty * 10**FRACTION_DIGITS) / 10**FRACTION_DIGITS
    return float(math.floor(qty))


def evaluate(
    limits: StockLimits,
    proposal: StockProposal,
    *,
    nav: float,
    open_positions: list[dict],
    quote: StockQuote,
    asset: Asset,
    clock: Clock,
    peak_nav: float,
    day_start_nav: float,
    halted: bool,
    next_earnings: date | None,
    allow_shorts: bool,
    max_share: float = 1.0,
    today: date | None = None,
) -> StockDecision:
    """Check a proposal against every hard limit and size it. Rejections list every reason.

    open_positions: dicts with symbol, qty (signed), value (absolute, USD) and risk (USD at the stop).
    max_share: the risk quant's limit on this side, as a share of normal size.
    """
    if halted:
        return StockDecision(False, reasons=["Desk is halted by the kill switch; Mithul must resume it."])
    stop_reason = kill_switch_reason(limits, nav, peak_nav)
    if stop_reason:
        return StockDecision(False, reasons=[stop_reason], kill_switch=True)

    reasons: list[str] = []
    long = proposal.side is Side.BUY
    if not long and not allow_shorts:
        reasons.append("Shorts are switched off: the bearish call is logged, no order.")

    loss = daily_loss_pct(nav, day_start_nav)
    if loss >= limits.daily_loss_limit_pct:
        reasons.append(f"Daily loss {loss:.2f}% reached the {limits.daily_loss_limit_pct}% limit.")

    if not clock.is_open:
        reasons.append("The market is closed.")
    else:
        opened = clock.next_close - timedelta(hours=6, minutes=30)  # a normal session; half days are shorter
        if clock.now - opened < timedelta(minutes=limits.no_entry_first_minutes):
            reasons.append(f"No new trades in the first {limits.no_entry_first_minutes} minutes of the session.")
        if clock.next_close - clock.now < timedelta(minutes=limits.no_entry_last_minutes):
            reasons.append(f"No new trades in the last {limits.no_entry_last_minutes} minutes of the session.")

    if not asset.tradable:
        reasons.append(f"{proposal.symbol} is not tradable at Alpaca right now.")
    if not long and not (asset.shortable and asset.easy_to_borrow):
        reasons.append(f"{proposal.symbol} can't be shorted now (not easy to borrow at Alpaca).")

    entry = quote.ask if long else quote.bid
    if long and proposal.stop_loss >= entry:
        reasons.append("Stop loss must be below the entry price on a buy.")
    if not long and proposal.stop_loss <= entry:
        reasons.append("Stop loss must be above the entry price on a short.")
    stop_pct = abs(entry - proposal.stop_loss) / entry * 100 if entry else 0.0
    if stop_pct < limits.min_stop_pct:
        reasons.append(f"Stop is {stop_pct:.2f}% away; the minimum is {limits.min_stop_pct}%.")
    if stop_pct > limits.max_stop_pct:
        reasons.append(f"Stop is {stop_pct:.2f}% away; the maximum is {limits.max_stop_pct}%.")
    if proposal.take_profit is not None and (proposal.take_profit - entry) * (1 if long else -1) <= 0:
        reasons.append("Take profit is on the wrong side of the entry price.")

    if quote.spread_pct > limits.max_spread_pct:
        reasons.append(f"Spread {quote.spread_pct:.2f}% is wider than {limits.max_spread_pct}%.")

    if next_earnings is not None:
        days = trading_days_until(today or clock.now.date(), next_earnings)
        if days <= limits.earnings_blackout_days:
            reasons.append(f"Earnings on {next_earnings} are {days} trading days away (blackout {limits.earnings_blackout_days}).")

    if len(open_positions) >= limits.max_open_positions:
        reasons.append(f"Already {len(open_positions)} open positions (max {limits.max_open_positions}).")
    if any(p["symbol"] == proposal.symbol for p in open_positions):
        reasons.append(f"A {proposal.symbol} position is already open.")

    equity = max(nav, 0.0)
    share = min(max(proposal.risk_fraction, 0.0), 1.0, max(max_share, 0.0))
    risk_budget = equity * limits.risk_per_trade_pct / 100 * share
    gross_now = sum(p["value"] for p in open_positions)
    max_value = max(0.0, min(equity * limits.max_position_pct / 100, equity * limits.max_gross_pct / 100 - gross_now))
    # Alpaca shorts only whole shares; longs can be fractional when the stock allows it.
    fractional = long and asset.fractionable
    qty = size(risk_budget, entry, proposal.stop_loss, max_value, fractional)
    risk_amount = qty * abs(entry - proposal.stop_loss)
    if qty <= 0:
        if not long and risk_budget / max(abs(entry - proposal.stop_loss), 1e-9) < 1:
            reasons.append(f"One share of {proposal.symbol} at {entry:.2f} with this stop risks "
                           f"{abs(entry - proposal.stop_loss):.2f} USD, more than the {risk_budget:.2f} USD allowed; "
                           "shorts must be whole shares.")
        else:
            reasons.append("Position size rounds to zero.")
    elif qty * entry < 1.0:
        reasons.append("Position is worth under 1 USD, Alpaca's minimum.")

    open_risk = sum(p["risk"] for p in open_positions)
    max_total = equity * limits.max_total_risk_pct / 100
    if open_risk + risk_amount > max_total + 1e-9:
        reasons.append(f"Total risk would be {open_risk + risk_amount:,.2f} USD, over the "
                       f"{limits.max_total_risk_pct}% cap ({max_total:,.2f}).")

    if reasons:
        return StockDecision(False, reasons=reasons)
    return StockDecision(True, qty=qty if long else -qty, risk_amount=round(risk_amount, 2))
