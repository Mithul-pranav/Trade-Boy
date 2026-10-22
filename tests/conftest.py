"""Tests must never reach Mithul's real Telegram chat, even when the bot token is set in the environment."""

import httpx
import pytest

from tradeboy.alerts import CHAT_VAR, TOKEN_VAR

_real_handle = httpx.HTTPTransport.handle_request


@pytest.fixture(autouse=True)
def no_real_telegram(monkeypatch):
    # Tests that exercise Telegram set their own fake token and pass a fake HTTP client.
    monkeypatch.delenv(TOKEN_VAR, raising=False)
    monkeypatch.delenv(CHAT_VAR, raising=False)

    # Fake clients use httpx.MockTransport, so only a real network send reaches this.
    def handle_request(self, request):
        if request.url.host == "api.telegram.org":
            raise AssertionError("A test tried to send a real Telegram message.")
        return _real_handle(self, request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_request)
