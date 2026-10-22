"""Thin client for the OANDA v20 REST API (practice or live)."""

from __future__ import annotations

from datetime import datetime

import httpx

from .config import OandaCredentials
from .models import AccountState, Candle, OpenTrade, Quote, currencies, price_decimals


class OandaError(RuntimeError):
    pass


def _parse_time(value: str) -> datetime:
    # OANDA returns RFC 3339 with nanoseconds; Python handles microseconds.
    head, _, frac = value.rstrip("Z").partition(".")
    return datetime.fromisoformat(f"{head}.{(frac or '0')[:6]}+00:00")


class OandaClient:
    def __init__(self, creds: OandaCredentials, http: httpx.Client | None = None):
        headers = {"Accept-Datetime-Format": "RFC3339"}
        if creds.token:
            headers["Authorization"] = f"Bearer {creds.token}"
        self.http = http or httpx.Client(base_url=creds.base_url, headers=headers, timeout=20)
        self.account_id = creds.account_id or self._only_account()

    def _only_account(self) -> str:
        accounts = self._request("GET", "/v3/accounts")["accounts"]
        if len(accounts) != 1:
            raise OandaError(f"Found {len(accounts)} accounts; set OANDA_ACCOUNT_ID to pick one.")
        return accounts[0]["id"]

    def _request(self, method: str, path: str, **kwargs) -> dict:
        resp = self.http.request(method, path, **kwargs)
        if resp.status_code >= 400:
            raise OandaError(f"OANDA {method} {path} failed ({resp.status_code}): {resp.text[:300]}")
        return resp.json()

    def account(self) -> AccountState:
        summary = self._request("GET", f"/v3/accounts/{self.account_id}/summary")["account"]
        return AccountState(
            nav=float(summary["NAV"]),
            balance=float(summary["balance"]),
            currency=summary["currency"],
            open_trades=self.open_trades(),
        )

    def open_trades(self) -> list[OpenTrade]:
        trades = self._request("GET", f"/v3/accounts/{self.account_id}/openTrades")["trades"]
        result = []
        for t in trades:
            stop = t.get("stopLossOrder", {}).get("price")
            result.append(
                OpenTrade(
                    trade_id=t["id"],
                    instrument=t["instrument"],
                    units=int(float(t["currentUnits"])),
                    entry_price=float(t["price"]),
                    stop_loss=float(stop) if stop else None,
                    # Filled in from the journal, which records risk at entry.
                    risk_amount=0.0,
                )
            )
        return result

    def quotes(self, instruments: list[str]) -> dict[str, Quote]:
        data = self._request(
            "GET",
            f"/v3/accounts/{self.account_id}/pricing",
            params={"instruments": ",".join(instruments), "includeHomeConversions": "true"},
        )
        loss_factor = {c["currency"]: float(c["accountLoss"]) for c in data.get("homeConversions", [])}
        quotes = {}
        for p in data["prices"]:
            _, quote_ccy = currencies(p["instrument"])
            quotes[p["instrument"]] = Quote(
                instrument=p["instrument"],
                bid=float(p["bids"][0]["price"]),
                ask=float(p["asks"][0]["price"]),
                quote_to_home_loss=loss_factor[quote_ccy],
            )
        return quotes

    def to_home_rate(self, currency: str, via_instrument: str) -> float:
        """Account-currency value of 1 unit of `currency`, read from a pair that contains it."""
        data = self._request(
            "GET",
            f"/v3/accounts/{self.account_id}/pricing",
            params={"instruments": via_instrument, "includeHomeConversions": "true"},
        )
        for c in data.get("homeConversions", []):
            if c["currency"] == currency:
                return float(c["positionValue"])
        raise OandaError(f"No home conversion for {currency} via {via_instrument}")

    def candles(self, instrument: str, granularity: str = "M15", count: int = 200) -> list[Candle]:
        data = self._request(
            "GET",
            f"/v3/instruments/{instrument}/candles",
            params={"granularity": granularity, "count": count, "price": "M"},
        )
        return [
            Candle(
                time=_parse_time(c["time"]),
                open=float(c["mid"]["o"]),
                high=float(c["mid"]["h"]),
                low=float(c["mid"]["l"]),
                close=float(c["mid"]["c"]),
                volume=int(c["volume"]),
            )
            for c in data["candles"]
            if c.get("complete", True)
        ]

    def price_at(self, instrument: str, when: datetime) -> float | None:
        """Mid close of the first 5-minute candle at or after `when`, or None if it hasn't happened yet."""
        data = self._request(
            "GET",
            f"/v3/instruments/{instrument}/candles",
            params={"granularity": "M5", "count": 1, "price": "M", "from": when.isoformat()},
        )
        candles = [c for c in data["candles"] if c.get("complete", True)]
        return float(candles[0]["mid"]["c"]) if candles else None

    def position_book(self, instrument: str) -> dict:
        """OANDA clients' open positions near the current price: share of long and short traders."""
        book = self._request("GET", f"/v3/instruments/{instrument}/positionBook")["positionBook"]
        price = float(book["price"])
        long_pct = sum(float(b["longCountPercent"]) for b in book["buckets"])
        short_pct = sum(float(b["shortCountPercent"]) for b in book["buckets"])
        # Where the crowd is trapped: positions held at a loss relative to the current price.
        long_underwater = sum(float(b["longCountPercent"]) for b in book["buckets"] if float(b["price"]) > price)
        short_underwater = sum(float(b["shortCountPercent"]) for b in book["buckets"] if float(b["price"]) < price)
        total = long_pct + short_pct or 1.0
        return {
            "time": book["time"],
            "price": price,
            "retail_long_pct": round(100 * long_pct / total, 1),
            "retail_short_pct": round(100 * short_pct / total, 1),
            "longs_underwater_pct": round(100 * long_underwater / total, 1),
            "shorts_underwater_pct": round(100 * short_underwater / total, 1),
        }

    def market_order(
        self, instrument: str, units: int, stop_loss: float, take_profit: float | None = None
    ) -> dict:
        """Fill-or-kill market order with the stop loss (and target) held by OANDA."""
        digits = price_decimals(instrument)
        order = {
            "type": "MARKET",
            "instrument": instrument,
            "units": str(units),
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {"price": f"{stop_loss:.{digits}f}"},
        }
        if take_profit is not None:
            order["takeProfitOnFill"] = {"price": f"{take_profit:.{digits}f}"}
        data = self._request("POST", f"/v3/accounts/{self.account_id}/orders", json={"order": order})
        if "orderFillTransaction" not in data:
            reason = data.get("orderCancelTransaction", {}).get("reason", "unknown")
            raise OandaError(f"Order for {instrument} was not filled: {reason}")
        return data

    def close_trade(self, trade_id: str) -> dict:
        return self._request("PUT", f"/v3/accounts/{self.account_id}/trades/{trade_id}/close")

    def set_stop(self, trade_id: str, instrument: str, price: float) -> dict:
        """Moves a trade's stop loss (held by OANDA, so it works while the desk is offline)."""
        stop = {"price": f"{price:.{price_decimals(instrument)}f}", "timeInForce": "GTC"}
        return self._request("PUT", f"/v3/accounts/{self.account_id}/trades/{trade_id}/orders",
                             json={"stopLoss": stop})

    def trade_details(self, trade_id: str) -> dict:
        """State ("OPEN" or "CLOSED") and realized profit or loss of one trade, in the account currency."""
        trade = self._request("GET", f"/v3/accounts/{self.account_id}/trades/{trade_id}")["trade"]
        return {"state": trade["state"], "realized_pl": float(trade.get("realizedPL", 0.0)),
                "close_price": float(trade["averageClosePrice"]) if trade.get("averageClosePrice") else None}
