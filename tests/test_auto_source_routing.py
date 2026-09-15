import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import bot as bot_module
import translator


def _message(content: str = "Hello from a Chinese-labelled channel"):
    return SimpleNamespace(
        author=SimpleNamespace(
            bot=False,
            display_name="Tester",
            display_avatar=SimpleNamespace(url="https://example.invalid/avatar.png"),
        ),
        channel=SimpleNamespace(id=101),
        guild=SimpleNamespace(id=7),
        content=content,
        attachments=[],
        stickers=[],
        reference=None,
        embeds=[],
        id=9001,
    )


def test_on_message_batches_once_and_routes_by_language_key(monkeypatch):
    """Fan-out must deliver by canonical language key, not provider result order."""
    batch_calls = []
    deliveries = []

    def fake_translate_many(text, targets, glossary=None, substitutions=None):
        batch_calls.append((text, targets, glossary, substitutions))
        # Deliberately different from channel order: ja, en, ko.
        return {
            "ja": translator.TranslationOutcome("日本語", True),
            "en": translator.TranslationOutcome("Hello", True),
            "ko": translator.TranslationOutcome("한국어", True),
        }

    async def fake_send_pretranslated(outcome, target, webhook_url, *args, **kwargs):
        deliveries.append((target, webhook_url, outcome.text))
        return bot_module._ForwardResult(8001, outcome.text or "", outcome.provider_succeeded)

    async def legacy_translate_and_send(*args, **kwargs):
        raise AssertionError("on_message must batch before it fans out")

    monkeypatch.setattr(bot_module.bot, "process_commands", AsyncMock())
    monkeypatch.setattr(bot_module, "translate_many_with_status", fake_translate_many, raising=False)
    monkeypatch.setattr(bot_module, "_send_pretranslated", fake_send_pretranslated, raising=False)
    monkeypatch.setattr(bot_module, "_translate_and_send", legacy_translate_and_send)
    monkeypatch.setattr(bot_module, "_store_cluster", lambda cluster: None)
    monkeypatch.setattr(bot_module, "get_guild_glossary", lambda *args: {})
    monkeypatch.setattr(bot_module, "get_guild_substitutions", lambda *args: {})
    monkeypatch.setattr(
        bot_module,
        "channel_configs",
        {
            7: {
                101: {"lang": "zh-TW", "webhook_url": "source", "group": "default"},
                202: {"lang": "ko", "webhook_url": "ko-hook", "group": "default"},
                203: {"lang": "en", "webhook_url": "en-hook", "group": "default"},
                204: {"lang": "ja", "webhook_url": "ja-hook", "group": "default"},
            }
        },
    )

    asyncio.run(bot_module.on_message(_message()))

    assert batch_calls == [
        ("Hello from a Chinese-labelled channel", ["ko", "en", "ja"], {}, {})
    ]
    assert deliveries == [
        ("ko", "ko-hook", "한국어"),
        ("en", "en-hook", "Hello"),
        ("ja", "ja-hook", "日本語"),
    ]


def test_successful_equal_output_is_forwarded_without_failure_retry(monkeypatch):
    """Restoring an equality check must not misclassify a detected-language success."""
    content = "English already suitable for the English destination"
    scheduled = []

    class SuccessfulForward(tuple):
        translation_succeeded = True

    async def fake_send_pretranslated(*args, **kwargs):
        return SuccessfulForward((8002, content))

    def fake_create_task(coro):
        scheduled.append(coro)
        coro.close()

    monkeypatch.setattr(bot_module.bot, "process_commands", AsyncMock())
    monkeypatch.setattr(
        bot_module,
        "translate_many_with_status",
        lambda *args, **kwargs: {"en": translator.TranslationOutcome(content, True)},
    )
    monkeypatch.setattr(bot_module, "_send_pretranslated", fake_send_pretranslated)
    monkeypatch.setattr(bot_module, "_store_cluster", lambda cluster: None)
    monkeypatch.setattr(bot_module, "get_guild_glossary", lambda *args: {})
    monkeypatch.setattr(bot_module, "get_guild_substitutions", lambda *args: {})
    monkeypatch.setattr(bot_module.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(
        bot_module,
        "channel_configs",
        {
            7: {
                101: {"lang": "zh-TW", "webhook_url": "source", "group": "default"},
                202: {"lang": "en", "webhook_url": "target", "group": "default"},
            }
        },
    )

    asyncio.run(bot_module.on_message(_message(content)))

    assert scheduled == []


def test_equal_nonempty_provider_output_has_success_status(monkeypatch):
    """Removing explicit status must make same-language delivery ambiguous again."""
    chain = SimpleNamespace(translate=lambda text, source, target: text)
    monkeypatch.setattr(translator, "_get_provider_chain", lambda: chain)

    outcome = translator.translate_text_with_status(
        "Already English", "auto", "en", _use_cache=False
    )

    assert outcome.text == "Already English"
    assert outcome.provider_succeeded is True


def test_provider_failure_keeps_original_with_failure_status(monkeypatch):
    """Reporting a fallback as success would suppress the delayed retry."""
    chain = SimpleNamespace(translate=lambda text, source, target: None)
    monkeypatch.setattr(translator, "_get_provider_chain", lambda: chain)

    outcome = translator.translate_text_with_status(
        "需要稍後重試", "auto", "ko", _use_cache=False
    )

    assert outcome.text == "需要稍後重試"
    assert outcome.provider_succeeded is False


def test_forward_result_preserves_provider_success(monkeypatch):
    """Returning a plain tuple would lose the status needed by retry scheduling."""
    content = "Already English"

    class FakeWebhook:
        async def send(self, **kwargs):
            return SimpleNamespace(id=4321)

    monkeypatch.setattr(
        bot_module,
        "translate_text_with_status",
        lambda *args, **kwargs: translator.TranslationOutcome(content, True),
        raising=False,
    )
    monkeypatch.setattr(bot_module, "translate_text", lambda *args, **kwargs: content)
    monkeypatch.setattr(
        bot_module.discord.Webhook,
        "from_url",
        lambda *args, **kwargs: FakeWebhook(),
    )

    result = asyncio.run(
        bot_module._translate_and_send(
            content,
            "auto",
            "en",
            "https://example.invalid/webhook",
            "Tester",
            "https://example.invalid/avatar.png",
            [],
            [],
            None,
        )
    )

    assert tuple(result) == (4321, content)
    assert result.translation_succeeded is True


def test_retry_accepts_successful_equal_output(monkeypatch):
    """An equality check in retry would keep reporting a recovered call as failed."""
    content = "Already English"
    cluster = {"prefixes": {}, "contents": {}}

    class FakeWebhook:
        async def edit_message(self, *args, **kwargs):
            return None

    monkeypatch.setattr(
        bot_module,
        "translate_text_with_status",
        lambda *args, **kwargs: translator.TranslationOutcome(content, True),
    )
    monkeypatch.setattr(bot_module, "translate_text", lambda *args, **kwargs: content)
    monkeypatch.setattr(
        bot_module.discord.Webhook,
        "from_url",
        lambda *args, **kwargs: FakeWebhook(),
    )

    asyncio.run(
        bot_module._retry_translate(
            content,
            "auto",
            "en",
            "https://example.invalid/webhook",
            4321,
            202,
            cluster,
            delay=0,
        )
    )

    assert cluster["contents"][202] == content
