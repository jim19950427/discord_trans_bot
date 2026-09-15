import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import bot as bot_module
import glossary
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


def test_failed_translation_retry_waits_one_minute_by_default(monkeypatch):
    waited = []

    async def fake_sleep(delay):
        waited.append(delay)

    monkeypatch.setattr(bot_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        bot_module,
        "translate_text_with_status",
        lambda *args, **kwargs: translator.TranslationOutcome(None, False),
    )

    asyncio.run(
        bot_module._retry_translate(
            "需要稍後重試",
            "auto",
            "ko",
            "https://example.invalid/webhook",
            4321,
            202,
            {"prefixes": {}, "contents": {}},
        )
    )

    assert waited == [60]


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


@pytest.fixture
def routing_environment(monkeypatch):
    """Keep routing/cluster/delivery code real; replace only translation and Discord I/O."""
    state = SimpleNamespace(batch_calls=[], sends=[], edits=[], deletes=[], downloads=[])
    guild = SimpleNamespace(id=7)
    source = SimpleNamespace(id=101, guild=guild, fetch_message=AsyncMock())
    channels = {101: source}
    hook_ids = {"ko-hook": 8202, "en-hook": 8203, "ja-hook": 8204}

    def translate_many(text, targets, glossary=None, substitutions=None):
        state.batch_calls.append((text, list(targets)))
        # Different from the target order in every routing path below.
        return {
            "ja": translator.TranslationOutcome("日本語", True),
            "en": translator.TranslationOutcome("Hello", True),
            "ko": translator.TranslationOutcome("한국어", True),
        }

    class Session:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def get(self, url):
            state.downloads.append(url)
            return self

        async def read(self):
            return b"attachment bytes"

    class Webhook:
        def __init__(self, url):
            self.url = url

        async def send(self, **kwargs):
            state.sends.append((
                self.url, kwargs.get("content", ""),
                [file.filename for file in kwargs.get("files", [])],
            ))
            return SimpleNamespace(id=hook_ids[self.url])

        async def edit_message(self, message_id, **kwargs):
            state.edits.append((self.url, message_id, kwargs["content"]))

        async def delete_message(self, message_id):
            state.deletes.append((self.url, message_id))

    monkeypatch.setattr(bot_module, "channel_configs", {
        7: {
            101: {"lang": "zh-TW", "webhook_url": "source", "group": "default"},
            202: {"lang": "ko", "webhook_url": "ko-hook", "group": "default"},
            203: {"lang": "en", "webhook_url": "en-hook", "group": "default"},
            204: {"lang": "ja", "webhook_url": "ja-hook", "group": "default"},
        }
    })
    monkeypatch.setattr(bot_module, "_msg_clusters", {})
    monkeypatch.setattr(bot_module, "_thread_clusters", {})
    monkeypatch.setattr(bot_module, "_glossary_data", {})
    monkeypatch.setattr(bot_module, "_substitutions_data", {})
    monkeypatch.setattr(bot_module, "translate_many_with_status", translate_many)
    monkeypatch.setattr(bot_module, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(bot_module.bot, "process_commands", AsyncMock())
    monkeypatch.setattr(bot_module.bot, "get_channel", channels.get)
    monkeypatch.setattr(bot_module.bot._connection, "_guilds", {7: guild})
    monkeypatch.setattr(bot_module.bot._connection, "user", SimpleNamespace(id=999))
    monkeypatch.setattr(bot_module.aiohttp, "ClientSession", Session)
    monkeypatch.setattr(bot_module.discord.Webhook, "from_url", lambda url, **kwargs: Webhook(url))
    state.source = source
    state.channels = channels
    return state


def _existing_cluster(*, missing=False):
    return {
        "channels": {101: 9001} if missing else {101: 9001, 202: 7202, 203: 7203, 204: 7204},
        "contents": {101: "Original text"},
        "author": "Tester", "avatar_url": "",
        "source_ch": 101, "source_lang": "auto", "raw_forward": False,
        "att_names": {101: []}, "att_urls": {101: []}, "prefixes": {},
    }


@pytest.mark.parametrize("raw_forward", [False, True])
def test_source_message_edit_routes_by_language_and_bypasses_raw_forward(routing_environment, raw_forward):
    """Edits must route each language to its own existing message, and preserve raw mode."""
    state = routing_environment
    message = _message("\\ Keep this unchanged" if raw_forward else "Edited text")
    state.source.fetch_message.return_value = message
    cluster = _existing_cluster()
    bot_module._store_cluster(cluster)

    asyncio.run(bot_module.on_raw_message_edit(SimpleNamespace(channel_id=101, message_id=9001)))

    assert state.batch_calls == ([] if raw_forward else [("Edited text", ["ko", "en", "ja"])])
    assert state.edits == ([
        ("ko-hook", 7202, "Keep this unchanged"),
        ("en-hook", 7203, "Keep this unchanged"),
        ("ja-hook", 7204, "Keep this unchanged"),
    ] if raw_forward else [
        ("ko-hook", 7202, "한국어"),
        ("en-hook", 7203, "Hello"),
        ("ja-hook", 7204, "日本語"),
    ])
    assert cluster["contents"][202] == ("Keep this unchanged" if raw_forward else "한국어")
    assert cluster["raw_forward"] is raw_forward


@pytest.mark.parametrize("mode", ["translated", "raw_forward", "attachment_only"])
def test_attachment_change_resend_routes_by_language_and_preserves_bypasses(routing_environment, mode):
    """Replacing attachments must route the new translations and skip providers for bypasses."""
    state = routing_environment
    content = {"translated": "Edited text", "raw_forward": "\\ Keep this unchanged", "attachment_only": ""}[mode]
    message = _message(content)
    message.attachments = [SimpleNamespace(filename="new.png", url="https://example.invalid/new.png")]
    state.source.fetch_message.return_value = message
    cluster = _existing_cluster()
    bot_module._store_cluster(cluster)

    asyncio.run(bot_module.on_raw_message_edit(SimpleNamespace(channel_id=101, message_id=9001)))

    assert state.batch_calls == ([("Edited text", ["ko", "en", "ja"])] if mode == "translated" else [])
    assert state.sends == ({
        "translated": [("ko-hook", "한국어", ["new.png"]), ("en-hook", "Hello", ["new.png"]), ("ja-hook", "日本語", ["new.png"])],
        "raw_forward": [("ko-hook", "Keep this unchanged", ["new.png"]), ("en-hook", "Keep this unchanged", ["new.png"]), ("ja-hook", "Keep this unchanged", ["new.png"])],
        "attachment_only": [("ko-hook", "", ["new.png"]), ("en-hook", "", ["new.png"]), ("ja-hook", "", ["new.png"])],
    }[mode])
    assert state.deletes == [("ko-hook", 7202), ("en-hook", 7203), ("ja-hook", 7204)]
    assert state.downloads == ["https://example.invalid/new.png"] * 3
    assert cluster["channels"] == {101: 9001, 202: 8202, 203: 8203, 204: 8204}
    assert all(old_id not in bot_module._msg_clusters for old_id in (7202, 7203, 7204))
    assert cluster["raw_forward"] is (mode == "raw_forward")


def test_thread_name_creation_routes_by_language_key(routing_environment):
    """A reversed result mapping must not attach translated names to the wrong channel."""
    state = routing_environment
    for channel_id, thread_id in ((202, 6202), (203, 6203), (204, 6204)):
        channel = Mock(spec=bot_module.discord.TextChannel)
        channel.create_thread = AsyncMock(return_value=SimpleNamespace(id=thread_id))
        state.channels[channel_id] = channel
    thread = SimpleNamespace(guild=SimpleNamespace(id=7), owner_id=42, parent_id=101, id=6101, name="New discussion")

    asyncio.run(bot_module.on_thread_create(thread))

    assert state.batch_calls == [("New discussion", ["ko", "en", "ja"])]
    assert [(channel_id, state.channels[channel_id].create_thread.call_args.kwargs["name"]) for channel_id in (202, 203, 204)] == [
        (202, "한국어"), (203, "Hello"), (204, "日本語")
    ]
    assert bot_module._thread_clusters[6101] == {101: 6101, 202: 6202, 203: 6203, 204: 6204}


@pytest.mark.parametrize("mode", ["translated", "raw_forward", "attachment_only"])
def test_missing_channel_retry_routes_by_language_after_reload(routing_environment, monkeypatch, tmp_path, mode):
    """Backfills must retain language routing and persisted raw/attachment bypass behavior."""
    state = routing_environment
    cluster = _existing_cluster(missing=True)
    cluster["contents"][101] = "" if mode == "attachment_only" else "Keep this unchanged"
    cluster["raw_forward"] = mode == "raw_forward"
    cluster["att_names"][101] = ["saved.png"]
    cluster["att_urls"][101] = ["https://example.invalid/saved.png"]
    monkeypatch.setattr(glossary, "CLUSTERS_FILE", str(tmp_path / "clusters.json"))
    glossary.save_clusters({9001: cluster})
    restored = glossary.load_clusters()[9001]

    result = asyncio.run(bot_module._retry_missing_channels(restored, 7))

    assert result == ([202, 203, 204], [])
    assert state.batch_calls == ([("Keep this unchanged", ["ko", "en", "ja"])] if mode == "translated" else [])
    assert state.sends == ({
        "translated": [("ko-hook", "한국어", ["saved.png"]), ("en-hook", "Hello", ["saved.png"]), ("ja-hook", "日本語", ["saved.png"])],
        "raw_forward": [("ko-hook", "Keep this unchanged", ["saved.png"]), ("en-hook", "Keep this unchanged", ["saved.png"]), ("ja-hook", "Keep this unchanged", ["saved.png"])],
        "attachment_only": [("ko-hook", "", ["saved.png"]), ("en-hook", "", ["saved.png"]), ("ja-hook", "", ["saved.png"])],
    }[mode])
    assert state.downloads == ["https://example.invalid/saved.png"] * 3
    assert restored["channels"] == {101: 9001, 202: 8202, 203: 8203, 204: 8204}
    assert restored["att_urls"][202] == ["https://example.invalid/saved.png"]
    assert bot_module._msg_clusters[8202] is restored


def test_personal_context_translation_routes_by_language_key(routing_environment, monkeypatch):
    """Personal language labels must follow requested keys, including duplicate preferences."""
    state = routing_environment
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=42), guild_id=7,
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    monkeypatch.setattr(bot_module, "_user_langs_data", {"42": ["ko", "en", "ja", "ko"]})

    asyncio.run(bot_module.translate_context_menu.callback(interaction, _message("Personal request")))

    assert state.batch_calls == [("Personal request", ["ko", "en", "ja"])]
    sent = interaction.followup.send.call_args.kwargs
    assert [(field.name, field.value) for field in sent["embed"].fields] == [
        ("原文（自動偵測）", "Personal request"), ("ko", "한국어"), ("en", "Hello"), ("ja", "日本語")
    ]
    assert interaction.response.defer.call_args.kwargs == {"ephemeral": True}
    assert sent["ephemeral"] is True


@pytest.mark.parametrize("mode", ["raw_forward", "attachment_only"])
def test_on_message_bypasses_translation_for_raw_and_attachment_only(routing_environment, mode):
    """New raw/attachment messages must reach every target without a provider call."""
    state = routing_environment
    message = _message("\\ Keep this unchanged" if mode == "raw_forward" else "")
    message.attachments = [SimpleNamespace(filename="image.png", url="https://example.invalid/image.png")]

    asyncio.run(bot_module.on_message(message))

    assert state.batch_calls == []
    assert state.sends == ([
        ("ko-hook", "Keep this unchanged", ["image.png"]),
        ("en-hook", "Keep this unchanged", ["image.png"]),
        ("ja-hook", "Keep this unchanged", ["image.png"]),
    ] if mode == "raw_forward" else [
        ("ko-hook", "", ["image.png"]), ("en-hook", "", ["image.png"]), ("ja-hook", "", ["image.png"]),
    ])
    assert bot_module._msg_clusters[9001]["raw_forward"] is (mode == "raw_forward")
