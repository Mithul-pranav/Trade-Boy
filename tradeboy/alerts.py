"""Alerts to Mithul for the few things that need a human: the budget is gone, or the agents can't run."""

from __future__ import annotations

import logging
import os
from datetime import datetime

import httpx

from .journal import Journal

log = logging.getLogger("tradeboy")


class Alerts:
    """Sends each kind of alert at most once per day, by Telegram when it's set up, and always to the log.

    Telegram needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in the environment.
    """

    def __init__(self, journal: Journal, http: httpx.Client | None = None):
        self.journal = journal
        self.http = http or httpx.Client(timeout=20)
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    def send(self, kind: str, text: str, now: datetime) -> bool:
        key = f"alert:{kind}"
        today = now.date().isoformat()
        if self.journal.get_state(key) == today:
            return False
        self.journal.set_state(key, today)
        log.warning("ALERT %s: %s", kind, text)
        if self.token and self.chat_id:
            try:
                self.http.post(
                    f"https://api.telegram.org/bot{self.token}/sendMessage",
                    json={"chat_id": self.chat_id, "text": f"Trade-Boy: {text}"},
                ).raise_for_status()
            except httpx.HTTPError as exc:
                log.error("Telegram alert failed: %s", exc)
        return True
