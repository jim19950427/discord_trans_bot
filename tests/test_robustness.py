"""Retry persistence, webhook self-heal, command errors, liveness, and the
cluster-sync event handlers (delete / reaction / reconnect)."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from discord import app_commands
from discord.ext import commands

import bot as bot_module
import glossary


@pytest.fixture(autouse=True)
def quiet_logs(monkeypatch):
    monkeypatch.setattr(bot_module, "log_event", lambda *a, **k: None)


def run(coro):
    return asyncio.run(coro)


# --- #1 retries survive restarts and are never garbage-collected -------------

def test_spawn_keeps_strong_reference_until_done():
    async def scenario():
        gate = asyncio.Event()

        async def work():
            await gate.wait()

        task = bot_module._spawn(work())
        assert task in bot_module._background_tasks
        gate.set()
        await task
        await asyncio.sleep(0)
        assert task not in bot_module._background_tasks

    run(scenario())


def test_failed_background_task_is_logged(monkeypatch):
    logged = []
    monkeypatch.setattr(bot_module, "log_event", lambda m, **k: logged.append(m))

    async def boom():
        raise RuntimeError("kaboom")

    async def scenario():
        task = bot_module._spawn(boom())
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

    run(scenario())
    assert any("kaboom" in m for m in logged)


@pytest.fixture
def retry_env(tmp_path, monkeypatch):
    monkeypatch.setattr(glossary, "PENDING_RETRIES_FILE", str(tmp_path / "pending.json"))
    monkeypatch.setattr(bot_module, "_pending_retries", {})
    monkeypatch.setattr(bot_module, "_glossary_data", {})
    cluster = {"prefixes": {}, "contents": {}}
    monkeypatch.setattr(bot_module, "_msg_clusters", {4321: cluster})
    monkeypatch.setattr(bot_module, "channel_configs", {
        7: {202: {"lang": "ko", "webhook_url": "https://example.invalid/hook", "group": "default"}}
    })
    calls = []

    async def fake_retry(text, src, dest, webhook_url, msg_id, ch_id, cluster, glossary=None, delay=60):
        calls.append((text, dest, webhook_url, msg_id, ch_id, delay))

    monkeypatch.setattr(bot_module, "_retry_translate", fake_retry)
    return SimpleNamespace(calls=calls, file=tmp_path / "pending.json", cluster=cluster)


def test_scheduled_retry_is_persisted_without_webhook_url_then_cleared(retry_env):
    async def scenario():
        await bot_module._schedule_retry("hello", "auto", "ko", 7, 202, 4321, delay=0.05)
        saved = json.loads(retry_env.file.read_text())
        assert [e["msg_id"] for e in saved] == [4321]
        assert "webhook" not in retry_env.file.read_text()  # token-bearing URL never stored
        await asyncio.gather(*list(bot_module._background_tasks))

    run(scenario())
    assert retry_env.calls == [("hello", "ko", "https://example.invalid/hook", 4321, 202, 0)]
    assert json.loads(retry_env.file.read_text()) == []
    assert bot_module._pending_retries == {}


def test_pending_retries_are_restored_after_restart(retry_env):
    import time
    glossary.save_pending_retries([
        {"text": "hi", "src": "auto", "dest": "ko", "guild_id": 7,
         "ch_id": 202, "msg_id": 4321, "due": time.time() - 5},
        {"text": "too old", "src": "auto", "dest": "ko", "guild_id": 7,
         "ch_id": 202, "msg_id": 1, "due": time.time() - 7200},
    ])

    async def scenario():
        await bot_module._restore_pending_retries()
        await asyncio.gather(*list(bot_module._background_tasks))

    run(scenario())
    assert [c[0] for c in retry_env.calls] == ["hi"]  # stale one discarded


def test_retry_for_untracked_message_is_dropped(retry_env, monkeypatch):
    monkeypatch.setattr(bot_module, "_msg_clusters", {})  # evicted from cluster cache

    async def scenario():
        await bot_module._schedule_retry("hello", "auto", "ko", 7, 202, 4321, delay=0)
        await asyncio.gather(*list(bot_module._background_tasks))

    run(scenario())
    assert retry_env.calls == []
    assert json.loads(retry_env.file.read_text()) == []


def test_corrupt_pending_file_is_quarantined(tmp_path, monkeypatch):
    path = tmp_path / "pending.json"
    path.write_text("[{broken")
    monkeypatch.setattr(glossary, "PENDING_RETRIES_FILE", str(path))
    assert glossary.load_pending_retries() == []
    assert not path.exists() and any(".corrupt-" in p.name for p in tmp_path.iterdir())


def test_malformed_pending_entries_are_filtered(tmp_path, monkeypatch):
    path = tmp_path / "pending.json"
    path.write_text(json.dumps([{"text": "x"}, "junk", {
        "text": "ok", "src": "auto", "dest": "en", "guild_id": 1, "ch_id": 2, "msg_id": 3, "due": 1.5}]))
    monkeypatch.setattr(glossary, "PENDING_RETRIES_FILE", str(path))
    assert [e["text"] for e in glossary.load_pending_retries()] == ["ok"]


# --- #2 command errors are reported, not swallowed ---------------------------

def _http_exc(cls, status, code=0):
    response = SimpleNamespace(status=status, reason="x")
    return cls(response, {"code": code, "message": "m"})


@pytest.mark.parametrize("error,expected", [
    (app_commands.MissingPermissions(["manage_channels"]), "管理頻道"),
    (_http_exc(discord.Forbidden, 403), "缺少所需權限"),
    (_http_exc(discord.HTTPException, 500), "HTTP 500"),
    (RuntimeError("secret internals"), "未預期"),
])
def test_command_error_text_never_leaks_internals(error, expected):
    wrapped = app_commands.CommandInvokeError(SimpleNamespace(name="addlang"), error) \
        if not isinstance(error, app_commands.MissingPermissions) else error
    text = bot_module._command_error_text(wrapped)
    assert expected in text
    assert "secret internals" not in text


def _interaction(done):
    return SimpleNamespace(
        command=SimpleNamespace(name="addlang"), guild_id=7, channel_id=101,
        response=SimpleNamespace(is_done=lambda: done, send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def test_app_command_error_replies_ephemerally_and_logs(monkeypatch):
    logged = []
    monkeypatch.setattr(bot_module, "log_event", lambda m, **k: logged.append((m, k)))
    interaction = _interaction(done=False)
    error = app_commands.CommandInvokeError(
        SimpleNamespace(name="addlang"), _http_exc(discord.Forbidden, 403))

    run(bot_module._on_app_command_error(interaction, error))

    interaction.response.send_message.assert_awaited_once()
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True
    assert logged and logged[0][1]["type"] == "error" and logged[0][1]["command"] == "addlang"


def test_app_command_error_uses_followup_when_already_responded():
    interaction = _interaction(done=True)
    run(bot_module._on_app_command_error(interaction, app_commands.MissingPermissions(["manage_channels"])))
    interaction.followup.send.assert_awaited_once()
    interaction.response.send_message.assert_not_awaited()


def test_setlang_without_manage_webhooks_replies_instead_of_crashing(monkeypatch):
    target = SimpleNamespace(
        mention="#ch", id=5,
        webhooks=AsyncMock(side_effect=_http_exc(discord.Forbidden, 403)),
    )
    respond = AsyncMock()
    run(bot_module._do_setlang(7, target, "en", respond))
    assert "管理 Webhook" in respond.await_args.args[0]


def test_prefix_unknown_command_stays_silent_but_permission_error_replies():
    ctx = SimpleNamespace(command=None, guild=SimpleNamespace(id=7),
                          channel=SimpleNamespace(id=1), send=AsyncMock())
    run(bot_module.on_command_error(ctx, commands.CommandNotFound("nope")))
    ctx.send.assert_not_awaited()
    run(bot_module.on_command_error(ctx, commands.MissingPermissions(["manage_channels"])))
    assert "管理頻道" in ctx.send.await_args.args[0]


# --- #3 deleted webhooks are recreated once and the send is retried ----------

@pytest.fixture
def heal_env(monkeypatch):
    config = {7: {202: {"lang": "ko", "webhook_url": "dead-url", "group": "default"}}}
    monkeypatch.setattr(bot_module, "channel_configs", config)
    monkeypatch.setattr(bot_module, "_healed_urls", {})
    saved = []
    monkeypatch.setattr(bot_module, "save_channel_config", lambda c: saved.append(json.dumps(c)))
    channel = Mock(spec=discord.TextChannel)
    channel.webhooks = AsyncMock(return_value=[])
    channel.create_webhook = AsyncMock(return_value=SimpleNamespace(url="new-url"))
    monkeypatch.setattr(bot_module.bot, "get_channel", lambda cid: channel)
    sends = []

    class Webhook:
        def __init__(self, url):
            self.url = url

        async def send(self, **kwargs):
            files = kwargs.get("files")
            sends.append((self.url, [f.fp.read() for f in files] if files else None))
            if self.url == "dead-url":
                raise _http_exc(discord.NotFound, 404, code=10015)
            return SimpleNamespace(id=555)

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(bot_module.aiohttp, "ClientSession", Session)
    monkeypatch.setattr(bot_module.discord.Webhook, "from_url", lambda url, **k: Webhook(url))
    return SimpleNamespace(config=config, sends=sends, channel=channel, saved=saved)


def test_missing_webhook_is_recreated_and_files_survive_the_retry(heal_env):
    result = run(bot_module._webhook_send("dead-url", {"content": "hi"}, [("a.png", b"PNGDATA")]))

    assert result.id == 555
    assert [u for u, _ in heal_env.sends] == ["dead-url", "new-url"]
    assert heal_env.sends[1][1] == [b"PNGDATA"]  # file rebuilt, not a consumed handle
    assert heal_env.config[7][202]["webhook_url"] == "new-url"
    assert heal_env.saved and "new-url" in heal_env.saved[0]


def test_concurrent_senders_share_one_recreation(heal_env):
    async def scenario():
        return await asyncio.gather(*[
            bot_module._webhook_send("dead-url", {"content": str(i)}, []) for i in range(4)
        ])

    results = run(scenario())
    assert all(r.id == 555 for r in results)
    assert heal_env.channel.create_webhook.await_count == 1


def test_other_not_found_errors_are_not_treated_as_missing_webhook(heal_env, monkeypatch):
    class Webhook:
        async def send(self, **kwargs):
            raise _http_exc(discord.NotFound, 404, code=10003)  # unknown channel/thread

    monkeypatch.setattr(bot_module.discord.Webhook, "from_url", lambda url, **k: Webhook())
    with pytest.raises(discord.NotFound):
        run(bot_module._webhook_send("dead-url", {"content": "hi"}, []))
    heal_env.channel.create_webhook.assert_not_awaited()


def test_heal_gives_up_quietly_without_permission(heal_env):
    heal_env.channel.webhooks = AsyncMock(side_effect=_http_exc(discord.Forbidden, 403))
    with pytest.raises(discord.NotFound):
        run(bot_module._webhook_send("dead-url", {"content": "hi"}, []))
    assert heal_env.config[7][202]["webhook_url"] == "dead-url"


# --- #4 cluster sync handlers --------------------------------------------------

def _cluster():
    return {
        "channels": {101: 1, 202: 2, 203: 3},
        "contents": {101: "a", 202: "b", 203: "c"},
        "author": "T", "avatar_url": "", "source_ch": 101, "source_lang": "auto",
        "raw_forward": False, "prefixes": {}, "att_names": {}, "att_urls": {},
    }


@pytest.fixture
def delete_env(monkeypatch):
    cluster = _cluster()
    monkeypatch.setattr(bot_module, "_msg_clusters", {1: cluster, 2: cluster, 3: cluster})
    guild_channels = {
        101: {"lang": "zh-TW", "webhook_url": "h101", "group": "default"},
        202: {"lang": "ko", "webhook_url": "h202", "group": "default"},
        203: {"lang": "en", "webhook_url": "h203", "group": "default"},
    }
    monkeypatch.setattr(bot_module, "_guild_channels_for", lambda cid: guild_channels)
    deleted = []

    async def fake_delete(url, msg_id, ch_id):
        deleted.append((url, msg_id))
        # The bot's own delete fires the same event for the mirror message.
        await bot_module.on_raw_message_delete(SimpleNamespace(message_id=msg_id, channel_id=ch_id))

    monkeypatch.setattr(bot_module, "_delete_webhook_message", fake_delete)
    return SimpleNamespace(deleted=deleted)


def test_deleting_source_removes_each_mirror_exactly_once(delete_env):
    run(bot_module.on_raw_message_delete(SimpleNamespace(message_id=1, channel_id=101)))
    assert sorted(delete_env.deleted) == [("h202", 2), ("h203", 3)]
    assert bot_module._msg_clusters == {}


def test_deleting_a_mirror_removes_the_other_copies_without_looping(delete_env):
    run(bot_module.on_raw_message_delete(SimpleNamespace(message_id=2, channel_id=202)))
    assert sorted(delete_env.deleted) == [("h101", 1), ("h203", 3)]
    assert bot_module._msg_clusters == {}


def test_delete_of_untracked_message_does_nothing(delete_env):
    run(bot_module.on_raw_message_delete(SimpleNamespace(message_id=999, channel_id=101)))
    assert delete_env.deleted == []


def test_reaction_is_mirrored_to_every_other_copy(monkeypatch):
    cluster = _cluster()
    monkeypatch.setattr(bot_module, "_msg_clusters", {1: cluster, 2: cluster, 3: cluster})
    monkeypatch.setattr(bot_module.bot._connection, "user", SimpleNamespace(id=999))
    reacted = []
    channels = {}
    for ch_id, msg_id in cluster["channels"].items():
        msg = SimpleNamespace(add_reaction=AsyncMock(side_effect=lambda e, c=ch_id: reacted.append(c)))
        channels[ch_id] = SimpleNamespace(fetch_message=AsyncMock(return_value=msg))
    monkeypatch.setattr(bot_module.bot, "get_channel", channels.get)

    payload = SimpleNamespace(user_id=5, message_id=1, emoji="👍")
    run(bot_module.on_raw_reaction_add(payload))

    assert sorted(reacted) == [202, 203]  # not echoed back onto the origin


def test_bot_own_reaction_is_ignored(monkeypatch):
    monkeypatch.setattr(bot_module, "_msg_clusters", {1: _cluster()})
    monkeypatch.setattr(bot_module.bot._connection, "user", SimpleNamespace(id=999))
    monkeypatch.setattr(bot_module.bot, "get_channel", lambda cid: pytest.fail("must not fetch"))
    run(bot_module.on_raw_reaction_add(SimpleNamespace(user_id=999, message_id=1, emoji="👍")))


# --- #5 liveness ---------------------------------------------------------------

def test_watchdog_verdicts():
    v = bot_module._watchdog_verdict
    assert v(1000, 990, None) is None
    assert "event loop" in v(1000, 600, None)
    assert v(1000, 990, 900) is None                       # briefly disconnected
    assert "gateway" in v(2000, 1990, 900)                 # not ready for 1100s


def test_heartbeat_touch_creates_then_refreshes(tmp_path):
    import os
    path = tmp_path / "sub" / "heartbeat"
    bot_module._touch_heartbeat(str(path))
    os.utime(path, (1, 1))
    bot_module._touch_heartbeat(str(path))
    assert path.stat().st_mtime > 1000


def test_second_on_ready_keeps_live_state(monkeypatch):
    live = _cluster()
    monkeypatch.setattr(bot_module, "_msg_clusters", {1: live})
    monkeypatch.setattr(bot_module, "_startup_done", True)
    monkeypatch.setattr(bot_module, "load_clusters", lambda: pytest.fail("must not reload"))
    run(bot_module.on_ready())
    assert bot_module._msg_clusters[1] is live


def test_concurrent_retry_scheduling_persists_every_entry(retry_env, monkeypatch):
    monkeypatch.setattr(bot_module, "_msg_clusters", {i: dict(retry_env.cluster) for i in range(10, 20)})

    async def scenario():
        await asyncio.gather(*[
            bot_module._schedule_retry("t", "auto", "ko", 7, 202, i, delay=30) for i in range(10, 20)
        ])
        saved = json.loads(retry_env.file.read_text())
        for task in list(bot_module._background_tasks):
            task.cancel()
        await asyncio.gather(*list(bot_module._background_tasks), return_exceptions=True)
        return saved

    saved = run(scenario())
    assert sorted(e["msg_id"] for e in saved) == list(range(10, 20))
