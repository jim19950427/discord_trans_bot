import asyncio
from types import SimpleNamespace

import pytest

import bot as bot_module


def _healthy_status():
    return {
        "azure": {
            "configured": True,
            "circuit_state": "closed",
            "last_success_at": 1_700_000_000,
            "last_failure_at": None,
            "last_latency_ms": 42,
            "last_target_count": 3,
            "last_failure_reason": None,
            "unexpected_secret": "azure-key-should-never-appear",
        },
        "fallback": {"last_at": None, "reason": None},
        "libretranslate_probe": {
            "healthy": True,
            "languages": ["en", "ja", "ko", "zt"],
            "latency_ms": 17,
            "failure_reason": None,
            "webhook_url": "https://discord.example/webhook-secret",
        },
        "message": "private source text",
    }


def _embed_text(embed):
    payload = embed.to_dict()
    return "\n".join(
        [payload.get("title", ""), payload.get("description", "")]
        + [field["name"] + field["value"] for field in payload.get("fields", [])]
    )


def test_translation_status_embed_allowlists_healthy_health_fields():
    """Breaks if formatter serializes arbitrary façade keys into Discord output."""
    payload = bot_module._format_translation_status(_healthy_status()).to_dict()

    assert payload["title"] == "翻譯服務狀態"
    assert len(payload["fields"]) == 3
    assert "<t:1700000000:R>" in _embed_text(bot_module._format_translation_status(_healthy_status()))
    assert "42 ms" in _embed_text(bot_module._format_translation_status(_healthy_status()))
    assert "3" in _embed_text(bot_module._format_translation_status(_healthy_status()))
    output = _embed_text(bot_module._format_translation_status(_healthy_status()))
    assert "en、ja、ko、zt" in output
    assert "azure-key-should-never-appear" not in output
    assert "webhook-secret" not in output
    assert "private source text" not in output


def test_translation_status_shows_partial_response_fallback_reason():
    """A sanitized partial fallback must remain visible in the operational status."""
    status = _healthy_status()
    status["fallback"] = {"last_at": 1_700_000_010, "reason": "partial_response"}

    output = _embed_text(bot_module._format_translation_status(status))

    assert "partial_response" in output
    assert "<t:1700000010:R>" in output


def test_translation_status_embed_handles_unhealthy_and_empty_history():
    """Breaks if absent health history or a failed Libre probe crashes status output."""
    status = {
        "azure": {
            "configured": False,
            "circuit_state": "open",
            "last_success_at": None,
            "last_failure_at": None,
            "last_latency_ms": None,
            "last_target_count": None,
            "last_failure_reason": None,
        },
        "fallback": {"last_at": 1_700_000_010, "reason": "http_503"},
        "libretranslate_probe": {
            "healthy": False,
            "languages": [],
            "latency_ms": 9,
            "failure_reason": "http_503",
        },
    }

    output = _embed_text(bot_module._format_translation_status(status))

    assert output.count("尚無資料") >= 3
    assert "未設定" in output
    assert "open" in output
    assert "失敗" in output
    assert "http_503" in output
    assert "<t:1700000010:R>" in output
    assert "被動結果：尚無資料" in output


class _FakeResponse:
    def __init__(self):
        self.deferred = []

    async def defer(self, **kwargs):
        self.deferred.append(kwargs)


class _FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)


class _FakeInteraction:
    def __init__(self):
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()


def test_translation_status_command_defers_and_sends_ephemeral_allowlisted_embed(monkeypatch):
    """Breaks if the command exposes status publicly or skips the provider façade."""
    interaction = _FakeInteraction()
    monkeypatch.setattr(bot_module, "get_translation_status", lambda *, probe_libre: _healthy_status())

    asyncio.run(bot_module.slash_translation_status.callback(interaction))

    assert interaction.response.deferred == [{"ephemeral": True}]
    assert len(interaction.followup.sent) == 1
    sent = interaction.followup.sent[0]
    assert sent["ephemeral"] is True
    assert "azure-key-should-never-appear" not in _embed_text(sent["embed"])
    assert bot_module.slash_translation_status.checks


def test_translation_status_requires_manage_channels():
    """Breaks if a non-channel-manager can invoke the operational status command."""
    check = bot_module.slash_translation_status.checks[0]

    assert check(SimpleNamespace(permissions=SimpleNamespace(manage_channels=True))) is True
    with pytest.raises(bot_module.app_commands.MissingPermissions):
        check(SimpleNamespace(permissions=SimpleNamespace(manage_channels=False)))


def test_translation_status_command_returns_ephemeral_embed_when_facade_fails(monkeypatch):
    """Breaks if a status-query failure escapes after Discord has been deferred."""
    interaction = _FakeInteraction()

    def fail(*, probe_libre):
        raise RuntimeError("status backend unavailable")

    monkeypatch.setattr(bot_module, "get_translation_status", fail)

    asyncio.run(bot_module.slash_translation_status.callback(interaction))

    assert interaction.response.deferred == [{"ephemeral": True}]
    assert interaction.followup.sent[0]["ephemeral"] is True
    assert "暫時無法取得狀態" in interaction.followup.sent[0]["embed"].description
