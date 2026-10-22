import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from tradeboy.alerts import AlertError, Alerts
from tradeboy.journal import Journal

NOW = datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc)


def telegram(responses, sent):
    def handler(request):
        sent.append(request)
        method = request.url.path.rsplit("/", 1)[-1]
        return responses.get(method, httpx.Response(200, json={"ok": True, "result": {}}))
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_sends_once_a_day_to_the_configured_chat(monkeypatch):
    monkeypatch.setenv("TRADEBOY_TELEGRAM_BOT_TOKEN", "bot")
    monkeypatch.setenv("TRADEBOY_TELEGRAM_CHAT_ID", "123")
    sent = []
    alerts = Alerts(Journal(":memory:"), http=telegram({}, sent))
    assert alerts.send("credits", "out", NOW)
    assert not alerts.send("credits", "out", NOW + timedelta(hours=2))
    assert alerts.send("credits", "out", NOW + timedelta(days=1))
    assert len(sent) == 2 and b'"chat_id":"123"' in sent[0].content.replace(b" ", b"")


def test_finds_and_remembers_the_chat_from_the_latest_message(monkeypatch):
    monkeypatch.setenv("TRADEBOY_TELEGRAM_BOT_TOKEN", "bot")
    monkeypatch.delenv("TRADEBOY_TELEGRAM_CHAT_ID", raising=False)
    updates = {"getUpdates": httpx.Response(200, json={"ok": True, "result": [
        {"update_id": 1, "message": {"chat": {"id": 111}, "text": "hi"}},
        {"update_id": 2, "message": {"chat": {"id": 987654}, "text": "/start"}},
    ]})}
    journal, sent = Journal(":memory:"), []
    Alerts(journal, http=telegram(updates, sent)).telegram("test")
    assert sent[-1].url.path.endswith("/sendMessage") and b"987654" in sent[-1].content
    assert Alerts(journal).chat_id == "987654"


def test_no_message_to_the_bot_yet(monkeypatch):
    monkeypatch.setenv("TRADEBOY_TELEGRAM_BOT_TOKEN", "bot")
    monkeypatch.delenv("TRADEBOY_TELEGRAM_CHAT_ID", raising=False)
    empty = {"getUpdates": httpx.Response(200, json={"ok": True, "result": []})}
    with pytest.raises(AlertError, match="send it any message"):
        Alerts(Journal(":memory:"), http=telegram(empty, [])).telegram("test")


def test_failed_send_never_logs_the_token(monkeypatch, caplog):
    monkeypatch.setenv("TRADEBOY_TELEGRAM_BOT_TOKEN", "SECRET-TOKEN")
    monkeypatch.setenv("TRADEBOY_TELEGRAM_CHAT_ID", "123")
    down = {"sendMessage": httpx.Response(401, json={"ok": False})}
    with caplog.at_level(logging.INFO, logger="tradeboy"):
        assert Alerts(Journal(":memory:"), http=telegram(down, [])).send("halted", "x", NOW)
    assert "HTTP 401" in caplog.text and "SECRET-TOKEN" not in caplog.text


def test_without_a_token_alerts_only_go_to_the_log(monkeypatch):
    monkeypatch.delenv("TRADEBOY_TELEGRAM_BOT_TOKEN", raising=False)
    sent = []
    assert Alerts(Journal(":memory:"), http=telegram({}, sent)).send("halted", "x", NOW)
    assert sent == []
