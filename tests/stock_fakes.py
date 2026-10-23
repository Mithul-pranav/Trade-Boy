"""Scripted stand-ins for Alpaca, SEC EDGAR, Yahoo and the news feeds, for testing the stocks desk."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from tradeboy.stocks.config import StockSettings
from tradeboy.stocks.models import Asset, Bar, Clock, Filing, Position, StockQuote

# Thursday 2026-10-22, 11:00 New York time: the market is open.
NOW = datetime(2026, 10, 22, 15, 0, tzinfo=timezone.utc)


def settings(**kw) -> StockSettings:
    universe = {"NVDA": {"name": "Nvidia", "group": "semiconductors"}, "AMD": {"name": "AMD", "group": "semiconductors"},
                "MSFT": {"name": "Microsoft", "group": "platforms"}}
    return StockSettings(universe=universe, **kw)


def bars(start=100.0, step=0.2, n=120, end=NOW, hours=24, wiggle=1.0) -> list[Bar]:
    """Bars ending the bar before `end`, with a gentle trend and a daily range of about 2 * wiggle."""
    out, price = [], start
    t0 = end - timedelta(hours=hours * n)
    for i in range(n):
        o = price
        price = price + step + (0.3 if i % 2 else -0.3)
        out.append(Bar(t0 + timedelta(hours=hours * i), o, max(o, price) + wiggle, min(o, price) - wiggle, price, 1e6))
    return out


def view(stance="long", confidence=60, hold_days=5, **extra):
    return {"stance": stance, "confidence": confidence, "thesis": f"{stance} thesis", "evidence": ["e"],
            "invalidation": "x", "hold_days": hold_days, **extra}


def head(side="long", stop=None, target=None, answers=(), size=1.0, exit_type="target", hold_days=5):
    return {"side": side, "size": size, "stop_loss": stop, "take_profit": target, "hold_days": hold_days,
            "exit_plan": {"type": exit_type, "why_trade_exists": "w", "close_early_if": "c"},
            "objection_answers": [{"objection_id": o, "answer": "a", "resolved": r} for o, r in answers],
            "dissent": "d", "reasoning": "because"}


def script(price=124.0, side="long", **head_kw):
    from tests.fakes import rebuttal, risk, skeptic

    stop = price * 0.95 if side == "long" else price * 1.05
    target = price * 1.10 if side == "long" else price * 0.90
    return {
        "fundamentals": view(side), "news": view(side), "technical": view(side, stop_loss=stop, take_profit=target),
        "fundamentals_rebuttal": rebuttal(side), "news_rebuttal": rebuttal(side), "technical_rebuttal": rebuttal(side),
        "skeptic": skeptic(("O1", "serious")), "risk_manager": risk(1.0, 1.0),
        "head_of_desk": head(side, stop=head_kw.pop("stop", stop), target=head_kw.pop("target", target),
                             answers=[("O1", True)], **head_kw),
        "trade_reviewer": {"action": "hold", "reason": "thesis intact"},
        "scanner": {"picked": []},
    }


class FakeAlpaca:
    def __init__(self, equity=100_000.0, price=124.0, is_open=True, now=NOW):
        self.equity_value = equity
        self.price = {"NVDA": price, "AMD": price, "MSFT": price, "QQQ": 500.0}
        self.spread = 0.02
        self.held: dict[str, Position] = {}
        self.orders: list[dict] = []
        self.cancelled: list[str] = []
        self.closed: list[str] = []
        self.replaced: list[tuple[str, float]] = []
        self.daily = {s: bars(start=p * 0.8, step=p * 0.2 / 120) for s, p in self.price.items()}
        self.is_open = is_open
        self.now = now
        self.assets = {}
        self.later_price = None
        self._n = 0

    def account(self):
        return {"equity": self.equity_value, "cash": self.equity_value, "currency": "USD", "buying_power": 2 * self.equity_value,
                "status": "ACTIVE", "trading_blocked": False, "shorting_enabled": True, "account_number": "PA-TEST"}

    def positions(self):
        return list(self.held.values())

    def clock(self):
        close = self.now.replace(hour=20, minute=0)
        return Clock(self.now, self.is_open, self.now + timedelta(days=1), close)

    def asset(self, symbol):
        return self.assets.get(symbol, Asset(symbol))

    def quotes(self, symbols):
        return {s: StockQuote(s, self.price[s] - self.spread / 2, self.price[s] + self.spread / 2, self.price[s], self.now)
                for s in symbols if s in self.price}

    def bars(self, symbol, timeframe="1Day", limit=120, end=None):
        return self.daily[symbol][-limit:] if timeframe == "1Day" else bars(self.price[symbol] * 0.98, 0.01, limit, hours=1)

    def price_at(self, symbol, when):
        return self.later_price

    def _id(self):
        self._n += 1
        return f"order-{self._n}"

    def submit_order(self, symbol, qty, side, type_="market", stop_price=None, time_in_force="day"):
        order = {"id": self._id(), "symbol": symbol, "qty": qty, "side": side, "type": type_, "stop_price": stop_price,
                 "time_in_force": time_in_force, "status": "new"}
        if type_ == "market":
            fill = self.price[symbol] + (self.spread / 2 if side == "buy" else -self.spread / 2)
            order.update(status="filled", filled_qty=str(qty), filled_avg_price=str(fill), filled_at=self.now.isoformat())
            signed = qty if side == "buy" else -qty
            current = self.held.get(symbol)
            new_qty = (current.qty if current else 0) + signed
            if abs(new_qty) < 1e-9:
                self.held.pop(symbol, None)
            else:
                self.held[symbol] = Position(symbol, new_qty, fill, self.price[symbol])
        self.orders.append(order)
        return dict(order)

    def wait_for_fill(self, order_id, seconds=30, poll=1.0):
        return next(o for o in self.orders if o["id"] == order_id)

    def order(self, order_id):
        return next(o for o in self.orders if o["id"] == order_id)

    def open_orders(self, symbol=None):
        return [o for o in self.orders if o["status"] == "new" and o["id"] not in self.cancelled
                and (symbol is None or o["symbol"] == symbol)]

    def closed_orders(self, symbol, after=None):
        return [o for o in self.orders if o["symbol"] == symbol and o["status"] == "filled"]

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)

    def replace_stop(self, order_id, stop_price):
        self.replaced.append((order_id, stop_price))
        for o in self.orders:
            if o["id"] == order_id:
                o["stop_price"] = stop_price
        return {"id": order_id}

    def close_position(self, symbol):
        for o in self.open_orders(symbol):
            self.cancel_order(o["id"])
        p = self.held[symbol]
        self.closed.append(symbol)
        return self.submit_order(symbol, abs(p.qty), "sell" if p.qty > 0 else "buy")

    def stop_hit(self, symbol, price):
        """The stop order fills at Alpaca: the position is gone."""
        stop = self.open_orders(symbol)[0]
        stop.update(status="filled", filled_avg_price=str(price), filled_at=self.now.isoformat())
        self.held.pop(symbol)


class FakeEdgar:
    def __init__(self, filings=()):
        self.list = list(filings)
        self.texts_read = []

    def filings(self, symbol, now, forms=("8-K", "10-Q", "10-K"), days=30):
        return [f for f in self.list if f.symbol == symbol and f.form in forms]

    def filing_text(self, filing, max_chars=12000):
        self.texts_read.append(filing.accession)
        return "Revenue rose 40%. Guidance raised."

    def gaps(self, symbol):
        return []


def filing(symbol="NVDA", form="8-K", items=("2.02", "9.01"), accepted=NOW - timedelta(hours=2), accession="0001-26-000001"):
    return Filing(symbol, form, accession, accepted.date(), accepted, tuple(items), "https://www.sec.gov/x.htm")


class FakeEarnings:
    def __init__(self, dates=None):
        self.dates = dates or {}

    def next_earnings(self, symbol, now):
        return self.dates.get(symbol)


class FakeNews:
    def __init__(self, headlines=None):
        self.items = headlines or {}

    def headlines(self, symbols, now, hours=48, limit=25):
        from tradeboy.news import Headline

        out = []
        for s in symbols:
            out += [Headline(now - timedelta(minutes=i), f"Google News {s}", t, (s,)) for i, t in enumerate(self.items.get(s, []))]
        return out[:limit]

    def gaps(self, symbols=None):
        return []


def far_earnings():
    return FakeEarnings({"NVDA": date(2026, 11, 19), "AMD": date(2026, 11, 4), "MSFT": date(2027, 1, 28)})
