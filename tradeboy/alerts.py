"""Alerts to Mithul for the few things that need a human: the budget is gone, or the agents can't run."""

from __future__ import annotations

import logging
import os
from datetime import datetime

import httpx

from .journal import Journal

log = logging.getLogger("tradeboy")

TOKEN_VAR = "TRADEBOY_TELEGRAM_BOT_TOKEN"
CHAT_VAR = "TRADEBOY_TELEGRAM_CHAT_ID"


class AlertError(RuntimeError):
    pass


class Alerts:
    """Sends each kind of alert at most once per day, by Telegram when it's set up, and always to the log.

    Telegram needs TRADEBOY_TELEGRAM_BOT_TOKEN. The chat ID can be left out: it is then read from the
    latest message sent to the bot, and remembered.
    """

    def __init__(self, journal: Journal, http: httpx.Client | None = None):
        self.journal = journal
        self.http = http or httpx.Client(timeout=20)
        self.token = os.environ.get(TOKEN_VAR, "")
        self.chat_id = os.environ.get(CHAT_VAR, "") or journal.get_state("telegram_chat_id") or ""

    def _call(self, method: str, **payload) -> dict:
        # Errors never include the URL: it carries the bot token, which must stay out of the logs.
        try:
            resp = self.http.post(f"https://api.telegram.org/bot{self.token}/{method}", json=payload)
        except httpx.HTTPError as exc:
            raise AlertError(f"Telegram {method} failed: {type(exc).__name__}") from None
        if resp.status_code != 200:
            raise AlertError(f"Telegram {method} failed with HTTP {resp.status_code}")
        return resp.json()

    def find_chat(self) -> str:
        """The chat of the latest message sent to the bot. Mithul sends it any message once."""
        updates = self._call("getUpdates")["result"]
        chats = [u["message"]["chat"]["id"] for u in updates if "message" in u]
        if not chats:
            raise AlertError("No messages found. Open the bot in Telegram, press Start or send it any message, then retry.")
        self.chat_id = str(chats[-1])
        self.journal.set_state("telegram_chat_id", self.chat_id)
        return self.chat_id

    def telegram(self, text: str) -> None:
        if not self.token:
            raise AlertError(f"{TOKEN_VAR} is not set.")
        if not self.chat_id:
            self.find_chat()
        self._call("sendMessage", chat_id=self.chat_id, text=f"Trade-Boy: {text}")

    def send(self, kind: str, text: str, now: datetime) -> bool:
        key = f"alert:{kind}"
        today = now.date().isoformat()
        if self.journal.get_state(key) == today:
            return False
        self.journal.set_state(key, today)
        log.warning("ALERT %s: %s", kind, text)
        if self.token:
            try:
                self.telegram(text)
            except AlertError as exc:
                log.error("%s", exc)
        return True
