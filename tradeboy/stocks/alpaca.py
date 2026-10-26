"""Alpaca client for the stocks desk. PAPER TRADING ONLY: the live trading host is not in this file.

Orders, positions and the market clock come from the paper trading API; quotes and bars from Alpaca's
market data API (the free IEX feed). Keys come from TRADEBOY_ALPACA_KEY_ID and TRADEBOY_ALPACA_SECRET_KEY.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx

from .models import Asset, Bar, Clock, Position, StockQuote

PAPER_URL = "https://paper-api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"
KEY_VAR = "TRADEBOY_ALPACA_KEY_ID"
SECRET_VAR = "TRADEBOY_ALPACA_SECRET_KEY"
TIMEFRAMES = {"1Day": timedelta(days=1), "1Hour": timedelta(hours=1), "15Min": timedelta(minutes=15)}


class AlpacaError(RuntimeError):
    pass


def _time(text: str | None) -> datetime | None:
    if not text:
        return None
    # Alpaca sends RFC 3339 with up to nanoseconds; Python takes microseconds.
    text = re.sub(r"(\.\d{6})\d+", r"\1", text.replace("Z", "+00:00"))
    return datetime.fromisoformat(text)


class AlpacaPaper:
    def __init__(self, key_id: str | None = None, secret: str | None = None, http: httpx.Client | None = None,
                 feed: str = "iex"):
        key_id = key_id if key_id is not None else os.environ.get(KEY_VAR, "")
        secret = secret if secret is not None else os.environ.get(SECRET_VAR, "")
        if not (key_id and secret):
            raise AlpacaError(f"Set {KEY_VAR} and {SECRET_VAR} (Alpaca paper trading keys) in the environment.")
        self.http = http or httpx.Client(timeout=20)
        self.headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret}
        self.feed = feed

    # -- plumbing ------------------------------------------------------------------------------

    def _request(self, method: str, url: str, **kwargs):
        try:
            resp = self.http.request(method, url, headers=self.headers, **kwargs)
        except httpx.HTTPError as exc:
            raise AlpacaError(f"Alpaca {method} {url.split('.markets')[-1]} failed: {type(exc).__name__}") from None
        if resp.status_code >= 400:
            raise AlpacaError(f"Alpaca {method} {url.split('.markets')[-1]}: HTTP {resp.status_code} {resp.text[:300]}")
        return resp.json() if resp.content else {}

    def _trading(self, method: str, path: str, **kwargs):
        return self._request(method, PAPER_URL + path, **kwargs)

    def _data(self, path: str, **params):
        return self._request("GET", DATA_URL + path, params=params)

    # -- account -------------------------------------------------------------------------------

    def account(self) -> dict:
        raw = self._trading("GET", "/v2/account")
        return {
            "equity": float(raw["equity"]), "cash": float(raw["cash"]), "currency": raw.get("currency", "USD"),
            "buying_power": float(raw.get("buying_power", 0)), "status": raw.get("status", ""),
            "trading_blocked": bool(raw.get("trading_blocked")), "shorting_enabled": bool(raw.get("shorting_enabled")),
            "account_number": raw.get("account_number", ""),
        }

    def positions(self) -> list[Position]:
        return [
            Position(p["symbol"], float(p["qty"]), float(p["avg_entry_price"]), float(p.get("current_price") or 0))
            for p in self._trading("GET", "/v2/positions")
        ]

    def clock(self) -> Clock:
        raw = self._trading("GET", "/v2/clock")
        return Clock(_time(raw["timestamp"]), bool(raw["is_open"]), _time(raw["next_open"]), _time(raw["next_close"]))

    def trading_days(self, start: datetime, end: datetime) -> list[str]:
        rows = self._trading("GET", "/v2/calendar", params={"start": start.date().isoformat(), "end": end.date().isoformat()})
        return [r["date"] for r in rows]

    def asset(self, symbol: str) -> Asset:
        raw = self._trading("GET", f"/v2/assets/{symbol}")
        return Asset(symbol, bool(raw.get("tradable")), bool(raw.get("shortable")), bool(raw.get("easy_to_borrow")),
                     bool(raw.get("fractionable")))

    # -- market data ---------------------------------------------------------------------------

    def quotes(self, symbols: list[str]) -> dict[str, StockQuote]:
        joined = ",".join(symbols)
        quotes = self._data("/v2/stocks/quotes/latest", symbols=joined, feed=self.feed).get("quotes", {})
        trades = self._data("/v2/stocks/trades/latest", symbols=joined, feed=self.feed).get("trades", {})
        out = {}
        for s in symbols:
            q, t = quotes.get(s) or {}, trades.get(s) or {}
            last = float(t.get("p") or 0)
            bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
            if not (0 < bid <= ask):  # IEX sometimes shows one side only: fall back on the last trade
                bid = ask = last
            if bid > 0:
                out[s] = StockQuote(s, bid, ask, last or (bid + ask) / 2, _time(q.get("t") or t.get("t")))
        return out

    def bars(self, symbol: str, timeframe: str = "1Day", limit: int = 120, end: datetime | None = None) -> list[Bar]:
        end = end or datetime.now(timezone.utc)
        # Ask for enough calendar time to cover `limit` bars through weekends and nights.
        span = TIMEFRAMES[timeframe] * limit * (1.6 if timeframe == "1Day" else 4.5) + timedelta(days=4)
        params = {"symbols": symbol, "timeframe": timeframe, "start": (end - span).isoformat(), "end": end.isoformat(),
                  "limit": 10000, "feed": self.feed, "adjustment": "all", "sort": "asc"}
        rows: list[dict] = []
        while True:
            raw = self._data("/v2/stocks/bars", **params)
            rows.extend((raw.get("bars") or {}).get(symbol) or [])
            if not raw.get("next_page_token"):
                break
            params["page_token"] = raw["next_page_token"]
        return [Bar(_time(b["t"]), b["o"], b["h"], b["l"], b["c"], b["v"]) for b in rows][-limit:]

    def price_at(self, symbol: str, when: datetime) -> float | None:
        bars = self.bars(symbol, "1Hour", 30, end=when + timedelta(hours=1))
        before = [b for b in bars if b.time <= when]
        return before[-1].close if before else None

    # -- orders --------------------------------------------------------------------------------

    def submit_order(self, symbol: str, qty: float, side: str, type_: str = "market", stop_price: float | None = None,
                     time_in_force: str = "day") -> dict:
        body = {"symbol": symbol, "qty": f"{qty:g}" if qty == int(qty) else f"{qty:.6f}".rstrip("0"),
                "side": side, "type": type_, "time_in_force": time_in_force,
                "client_order_id": f"tb-{uuid.uuid4().hex[:20]}"}
        if stop_price is not None:
            body["stop_price"] = f"{stop_price:.2f}"
        return self._trading("POST", "/v2/orders", json=body)

    def order(self, order_id: str) -> dict:
        return self._trading("GET", f"/v2/orders/{order_id}")

    def open_orders(self, symbol: str | None = None) -> list[dict]:
        params = {"status": "open", "limit": 500, **({"symbols": symbol} if symbol else {})}
        return self._trading("GET", "/v2/orders", params=params)

    def closed_orders(self, symbol: str, after: str | None = None) -> list[dict]:
        params = {"status": "closed", "limit": 100, "symbols": symbol, "direction": "desc", **({"after": after} if after else {})}
        return self._trading("GET", "/v2/orders", params=params)

    def cancel_order(self, order_id: str) -> None:
        self._trading("DELETE", f"/v2/orders/{order_id}")

    def replace_stop(self, order_id: str, stop_price: float) -> dict:
        return self._trading("PATCH", f"/v2/orders/{order_id}", json={"stop_price": f"{stop_price:.2f}"})

    def wait_for_fill(self, order_id: str, seconds: float = 30, poll: float = 1.0) -> dict:
        deadline = time.monotonic() + seconds
        while True:
            order = self.order(order_id)
            if order["status"] in ("filled", "canceled", "expired", "rejected"):
                return order
            if time.monotonic() > deadline:
                return order
            time.sleep(poll)

    def close_position(self, symbol: str) -> dict:
        """Cancel the symbol's open orders (its stop holds the shares), then close at market."""
        for order in self.open_orders(symbol):
            self.cancel_order(order["id"])
        return self._trading("DELETE", f"/v2/positions/{symbol}")
