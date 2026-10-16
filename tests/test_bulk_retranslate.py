import asyncio
import datetime

import discord
import pytest

import bot as bot_module
from translator import TranslationOutcome


def run(coro):
    return asyncio.run(coro)


def _sid(hours_ago: float) -> int:
    when = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours_ago)
    return discord.utils.time_snowflake(when)


GROUP = {
    1: {"lang": "zh-TW", "webhook_url": "u1", "group": "default"},
    2: {"lang": "en", "webhook_url": "u2", "group": "default"},
    3: {"lang": "ja", "webhook_url": "u3", "group": "default"},
}


def _cluster(source_id, contents, **extra):
    channels = {1: source_id, 2: source_id + 1, 3: source_id + 2}
    return {"source_ch": 1, "channels": channels, "contents": contents, **extra}


@pytest.fixture
def env(monkeypatch):
    edits, calls = [], []
    monkeypatch.setattr(bot_module, "channel_configs", {7: GROUP})
    monkeypatch.setattr(bot_module, "_guild_channels_for", lambda _ch: GROUP)
    monkeypatch.setattr(bot_module, "_msg_clusters", {})
    monkeypatch.setattr(bot_module, "log_event", lambda *a, **k: None)

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(bot_module.asyncio, "sleep", no_sleep)

    def fake_translate(text, targets, glossary=None, subs=None):
        calls.append(list(targets))
        return {t: TranslationOutcome(f"{text}-{t}", True) for t in targets}

    monkeypatch.setattr(bot_module, "translate_many_with_status", fake_translate)

    async def fake_edit(url, msg_id, ch_id, cluster, content):
        edits.append((ch_id, msg_id, content))

    monkeypatch.setattr(bot_module, "_edit_message_parts", fake_edit)
    return edits, calls


def _register(*clusters):
    for cluster in clusters:
        for msg_id in cluster["channels"].values():
            bot_module._msg_clusters[msg_id] = cluster


def test_only_untranslated_copies_inside_the_window_are_retranslated(env):
    edits, calls = env
    sid = _sid(1)
    outage = _cluster(sid, {1: "你好", 2: "你好", 3: "こんにちは"})
    old = _cluster(_sid(10), {1: "舊", 2: "舊", 3: "舊"})
    _register(outage, old)

    stats = run(bot_module._bulk_retranslate(7, datetime.datetime.now().timestamp() - 2 * 3600))

    assert calls == [["en"]], "one batch per message, only the untranslated copy"
    assert edits == [(2, sid + 1, "你好-en")]
    assert outage["contents"][2] == "你好-en"
    assert old["contents"][2] == "舊", "messages outside the window are left alone"
    assert stats == {"messages": 1, "updated": 1, "failed": 0}


def test_reply_prefix_is_kept_and_raw_forwards_are_skipped(env):
    edits, calls = env
    since = datetime.datetime.now().timestamp() - 3600
    replied = _cluster(_sid(0.5), {1: "hi", 2: "hi", 3: "hi"}, prefixes={2: "> **A**: q"})
    raw = _cluster(_sid(0.4), {1: "raw", 2: "raw", 3: "raw"}, raw_forward=True)
    _register(replied, raw)

    stats = run(bot_module._bulk_retranslate(7, since))

    assert calls == [["en", "ja"]]
    assert (2, replied["channels"][2], "> **A**: q\nhi-en") in edits
    assert raw["contents"][2] == "raw"
    assert stats["messages"] == 1


def test_provider_failure_counts_as_failed_and_leaves_the_copy(env, monkeypatch):
    edits, _ = env
    monkeypatch.setattr(
        bot_module, "translate_many_with_status",
        lambda text, targets, g=None, s=None: {t: TranslationOutcome(text, False) for t in targets},
    )
    cluster = _cluster(_sid(0.5), {1: "你好", 2: "你好", 3: "你好"})
    _register(cluster)

    stats = run(bot_module._bulk_retranslate(7, datetime.datetime.now().timestamp() - 3600))

    assert edits == []
    assert cluster["contents"][2] == "你好"
    assert stats == {"messages": 1, "updated": 0, "failed": 2}


def test_same_language_copy_is_not_edited(env, monkeypatch):
    edits, _ = env
    monkeypatch.setattr(
        bot_module, "translate_many_with_status",
        lambda text, targets, g=None, s=None: {t: TranslationOutcome(text, True) for t in targets},
    )
    cluster = _cluster(_sid(0.5), {1: "hello", 2: "hello", 3: "hello"})
    _register(cluster)

    stats = run(bot_module._bulk_retranslate(7, datetime.datetime.now().timestamp() - 3600))

    assert edits == []
    assert stats["failed"] == 0


def test_edit_failure_is_counted_and_does_not_stop_other_copies(env, monkeypatch):
    edits, _ = env

    async def flaky(url, msg_id, ch_id, cluster, content):
        if ch_id == 2:
            raise RuntimeError("boom")
        edits.append(ch_id)

    monkeypatch.setattr(bot_module, "_edit_message_parts", flaky)
    cluster = _cluster(_sid(0.5), {1: "你好", 2: "你好", 3: "你好"})
    _register(cluster)

    stats = run(bot_module._bulk_retranslate(7, datetime.datetime.now().timestamp() - 3600))

    assert edits == [3]
    assert cluster["contents"][2] == "你好"
    assert stats == {"messages": 1, "updated": 1, "failed": 1}
