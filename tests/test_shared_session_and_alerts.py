import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

import bot as bot_module
import translator


def run(coro):
    return asyncio.run(coro)


# ---- shared HTTP session -------------------------------------------------

@pytest.fixture(autouse=True)
def _fresh_session_state(monkeypatch):
    monkeypatch.setattr(bot_module, "_http_session", None)
    monkeypatch.setattr(bot_module, "_http_session_loop", None)


def test_session_is_reused_within_a_loop_and_recreated_when_closed():
    async def scenario():
        first = bot_module._get_http_session()
        assert bot_module._get_http_session() is first
        await first.close()
        second = bot_module._get_http_session()
        assert second is not first and not second.closed
        await second.close()

    run(scenario())


def test_session_is_recreated_for_a_new_event_loop():
    seen = []

    async def grab():
        s = bot_module._get_http_session()
        seen.append(s)
        await s.close()

    run(grab())
    run(grab())
    assert seen[0] is not seen[1]


def test_webhook_calls_share_one_session(monkeypatch):
    sessions = []

    class Webhook:
        async def delete_message(self, msg_id):
            return None

        async def edit_message(self, msg_id, **kwargs):
            return None

    def from_url(url, session=None, **kwargs):
        sessions.append(session)
        return Webhook()

    monkeypatch.setattr(bot_module.discord.Webhook, "from_url", from_url)
    monkeypatch.setattr(bot_module, "log_event", lambda *a, **k: None)

    async def scenario():
        await bot_module._delete_webhook_message("u1", 1, 10)
        await bot_module._delete_webhook_message("u2", 2, 11)
        await bot_module._edit_pretranslated(
            bot_module.TranslationOutcome("hi", True), "u3", 3, 12, {"contents": {}, "prefixes": {}}
        )
        await bot_module._http_session.close()

    run(scenario())

    assert len(sessions) == 3
    assert all(isinstance(s, aiohttp.ClientSession) for s in sessions)
    assert sessions[0] is sessions[1] is sessions[2]


# ---- alert limiter -------------------------------------------------------

def test_same_alert_key_is_rate_limited_and_others_pass():
    limiter = bot_module._AlertLimiter(min_interval=600, max_per_hour=100)

    assert limiter.allow("a", 0) == (True, 0)
    assert limiter.allow("a", 100) == (False, 0)
    assert limiter.allow("b", 100) == (True, 1)  # reports the one suppressed
    assert limiter.allow("a", 601) == (True, 0)


def test_hourly_cap_limits_total_alerts_then_recovers():
    limiter = bot_module._AlertLimiter(min_interval=0, max_per_hour=2)

    assert limiter.allow("a", 0)[0] and limiter.allow("b", 1)[0]
    assert limiter.allow("c", 2)[0] is False
    assert limiter.allow("c", 3601)[0] is True


def test_alert_text_redacts_webhook_urls_and_truncates():
    text = bot_module._alert_text(
        "send failed https://discord.com/api/webhooks/123/SECRETTOKEN trailing " + "x" * 400
    )

    assert "SECRETTOKEN" not in text and "<webhook>" in text
    assert len(text) < 360


# ---- alert hook / delivery ----------------------------------------------

def test_error_hook_from_a_worker_thread_sends_an_alert(monkeypatch):
    sent = []
    monkeypatch.setattr(bot_module, "ALERT_CHANNEL_ID", 555)
    monkeypatch.setattr(bot_module, "_alert_limiter", bot_module._AlertLimiter(600, 10))
    monkeypatch.setattr(bot_module, "_send_alert", AsyncMock(side_effect=lambda t: sent.append(t)))

    async def scenario():
        monkeypatch.setattr(bot_module, "_alert_loop", asyncio.get_running_loop())
        t = threading.Thread(target=bot_module._error_alert_hook, args=("Failed to persist msg clusters: boom", {}))
        t.start()
        t.join()
        await asyncio.sleep(0.05)

    run(scenario())

    assert len(sent) == 1 and "Failed to persist msg clusters" in sent[0]


def test_error_hook_is_silent_when_no_alert_channel_is_configured(monkeypatch):
    monkeypatch.setattr(bot_module, "ALERT_CHANNEL_ID", 0)
    monkeypatch.setattr(bot_module, "_alert_loop", object())  # would blow up if touched

    bot_module._error_alert_hook("anything", {})


def test_failed_alert_delivery_is_logged_as_info_not_error(monkeypatch):
    logged = []
    monkeypatch.setattr(bot_module, "ALERT_CHANNEL_ID", 555)
    monkeypatch.setattr(bot_module, "log_event", lambda msg, **kw: logged.append((msg, kw)))
    monkeypatch.setattr(bot_module.bot, "get_channel", lambda _id: None)
    monkeypatch.setattr(bot_module.bot, "fetch_channel", AsyncMock(side_effect=RuntimeError("nope")))

    run(bot_module._send_alert("hello"))

    assert logged and all(kw.get("type", "info") != "error" for _, kw in logged)


# ---- log_event hook ------------------------------------------------------

def test_log_event_calls_hook_only_for_errors_and_survives_hook_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(translator, "LOG_FILE", str(tmp_path / "log.jsonl"))
    calls = []
    translator.set_error_hook(lambda msg, fields: calls.append(msg))
    try:
        translator.log_event("fine")
        translator.log_event("broken", type="error")
        assert calls == ["broken"]

        def boom(msg, fields):
            raise RuntimeError("hook bug")

        translator.set_error_hook(boom)
        translator.log_event("still logged", type="error")  # must not raise
    finally:
        translator.set_error_hook(None)


# ---- watchdog restart notice --------------------------------------------

def test_watchdog_leaves_its_reason_before_exiting(tmp_path, monkeypatch):
    reason_file = tmp_path / "reason"
    monkeypatch.setattr(bot_module, "RESTART_REASON_FILE", str(reason_file))
    monkeypatch.setattr(bot_module, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(bot_module, "_watchdog_verdict", lambda *a, **k: "event loop silent for over 300s")
    monkeypatch.setattr(bot_module, "time", SimpleNamespace(sleep=lambda s: None, monotonic=lambda: 0.0))
    monkeypatch.setattr(bot_module.bot, "is_ready", lambda: True)

    class Exited(Exception):
        pass

    def fake_exit(code):
        raise Exited(code)

    monkeypatch.setattr(bot_module.os, "_exit", fake_exit)

    with pytest.raises(Exited):
        bot_module._watchdog()

    assert reason_file.read_text() == "event loop silent for over 300s"


def test_restart_reason_is_announced_once_then_cleared(tmp_path, monkeypatch):
    reason_file = tmp_path / "reason"
    reason_file.write_text("gateway not ready for over 900s")
    monkeypatch.setattr(bot_module, "RESTART_REASON_FILE", str(reason_file))
    send = AsyncMock()
    monkeypatch.setattr(bot_module, "_send_alert", send)

    run(bot_module._announce_watchdog_restart())
    run(bot_module._announce_watchdog_restart())

    assert send.await_count == 1
    assert "gateway not ready" in send.await_args.args[0]
    assert not reason_file.exists()
