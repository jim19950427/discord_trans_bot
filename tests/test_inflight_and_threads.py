import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot as bot_module


def run(coro):
    return asyncio.run(coro)


def _user_message(mid=9001):
    return SimpleNamespace(id=mid, author=SimpleNamespace(bot=False))


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(bot_module, "_inflight_forwards", {})
    monkeypatch.setattr(bot_module, "_msg_clusters", {})
    monkeypatch.setattr(bot_module, "_own_message_ids", {})


# ---- in-flight forwards ------------------------------------------------------

def test_on_message_marks_the_message_in_flight_until_forwarding_ends(monkeypatch):
    seen = {}

    async def fake_forward(message):
        seen["during"] = message.id in bot_module._inflight_forwards

    monkeypatch.setattr(bot_module, "_forward_message", fake_forward)

    run(bot_module.on_message(_user_message()))

    assert seen["during"] is True
    assert bot_module._inflight_forwards == {}


def test_in_flight_marker_is_cleared_even_if_forwarding_raises(monkeypatch):
    async def boom(message):
        raise RuntimeError("send blew up")

    monkeypatch.setattr(bot_module, "_forward_message", boom)

    with pytest.raises(RuntimeError):
        run(bot_module.on_message(_user_message()))
    assert bot_module._inflight_forwards == {}


def _edit_env(monkeypatch, logged):
    channel = SimpleNamespace(
        id=101, guild=SimpleNamespace(id=7),
        fetch_message=AsyncMock(return_value=SimpleNamespace(author=SimpleNamespace(bot=True))),
    )
    monkeypatch.setattr(bot_module.bot, "get_channel", lambda _id: channel)
    monkeypatch.setattr(bot_module, "channel_configs", {7: {101: {"lang": "zh-TW", "group": "default"}}})
    monkeypatch.setattr(bot_module, "log_event", lambda msg, **kw: logged.append(msg))
    return channel


def test_edit_during_forwarding_waits_for_the_cluster_instead_of_being_dropped(monkeypatch):
    logged = []
    channel = _edit_env(monkeypatch, logged)
    cluster = {"channels": {101: 9001}, "contents": {}}

    async def scenario():
        done = asyncio.Event()
        bot_module._inflight_forwards[9001] = done
        edit = asyncio.create_task(bot_module.on_raw_message_edit(
            SimpleNamespace(channel_id=101, message_id=9001, data={})))
        await asyncio.sleep(0.02)
        assert not edit.done(), "the edit must wait while the message is still forwarding"
        bot_module._msg_clusters[9001] = cluster   # forwarding finished
        done.set()
        await edit

    run(scenario())

    assert not any(m.startswith("Edit ignored") for m in logged)
    channel.fetch_message.assert_awaited_once()   # went on to process the edit


def test_wait_gives_up_after_the_timeout(monkeypatch):
    logged = []
    _edit_env(monkeypatch, logged)
    monkeypatch.setattr(bot_module, "_INFLIGHT_WAIT_SECONDS", 0.01)

    async def scenario():
        bot_module._inflight_forwards[9001] = asyncio.Event()
        await bot_module.on_raw_message_edit(SimpleNamespace(channel_id=101, message_id=9001, data={}))

    run(scenario())

    assert any("gave up waiting" in m for m in logged)
    assert any(m.startswith("Edit ignored") for m in logged)


def test_delete_during_forwarding_removes_the_mirrors_once_they_exist(monkeypatch):
    deleted = []
    cluster = {"channels": {101: 9001, 202: 7202}, "contents": {}}
    monkeypatch.setattr(bot_module, "_guild_channels_for", lambda ch: {202: {"webhook_url": "hook"}})

    async def fake_delete(url, msg_id, ch_id, thread_id=None):
        deleted.append(msg_id)

    monkeypatch.setattr(bot_module, "_delete_webhook_message", fake_delete)

    async def scenario():
        done = asyncio.Event()
        bot_module._inflight_forwards[9001] = done
        deletion = asyncio.create_task(bot_module.on_raw_message_delete(
            SimpleNamespace(message_id=9001, channel_id=101)))
        await asyncio.sleep(0.02)
        for mid in (9001, 7202):
            bot_module._msg_clusters[mid] = cluster
        done.set()
        await deletion

    run(scenario())

    assert deleted == [7202]


def test_messages_not_in_flight_do_not_wait(monkeypatch):
    logged = []
    _edit_env(monkeypatch, logged)
    monkeypatch.setattr(bot_module, "_INFLIGHT_WAIT_SECONDS", 5)

    async def scenario():
        await asyncio.wait_for(bot_module.on_raw_message_edit(
            SimpleNamespace(channel_id=101, message_id=4242, data={})), 1)

    run(scenario())
    assert any(m.startswith("Edit ignored") for m in logged)


# ---- thread routing -----------------------------------------------------------

class _Webhook:
    def __init__(self, calls):
        self.calls = calls

    async def delete_message(self, msg_id, **kwargs):
        self.calls.append(("delete", msg_id, kwargs))

    async def edit_message(self, msg_id, **kwargs):
        self.calls.append(("edit", msg_id, kwargs))


@pytest.fixture
def hook_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(bot_module.discord.Webhook, "from_url", lambda url, **k: _Webhook(calls))
    monkeypatch.setattr(bot_module, "log_event", lambda *a, **k: None)
    return calls


def test_delete_names_the_thread_only_for_thread_messages(hook_calls):
    run(bot_module._delete_webhook_message("hook", 1, 202, 6202))
    run(bot_module._delete_webhook_message("hook", 2, 202))

    (_, _, with_thread), (_, _, without) = hook_calls
    assert with_thread["thread"].id == 6202
    assert without == {}


def test_edit_parts_in_a_thread_names_the_thread_for_edits_and_new_parts(hook_calls, monkeypatch):
    sends = []

    async def fake_send(url, kwargs, files):
        sends.append(kwargs)
        return SimpleNamespace(id=5000 + len(sends))

    monkeypatch.setattr(bot_module, "_webhook_send", fake_send)
    cluster = {"channels": {101: 9001, 202: 20}, "contents": {}, "author": "Ann", "avatar_url": "",
               "thread_channels": {101: 6101, 202: 6202}}

    run(bot_module._edit_message_parts("hook", 20, 202, cluster, "\n".join(["w" * 1500] * 2)))

    assert hook_calls[0][0] == "edit" and hook_calls[0][2]["thread"].id == 6202
    assert sends[0]["thread"].id == 6202


def test_deleting_a_thread_message_deletes_each_mirror_in_its_own_thread(monkeypatch):
    deleted = []
    cluster = {"channels": {101: 9001, 202: 7202, 203: 7203}, "contents": {},
               "thread_channels": {101: 6101, 202: 6202, 203: 6203}}
    bot_module._msg_clusters.update({9001: cluster, 7202: cluster, 7203: cluster})
    monkeypatch.setattr(bot_module, "_guild_channels_for",
                        lambda ch: {202: {"webhook_url": "h2"}, 203: {"webhook_url": "h3"}})

    async def fake_delete(url, msg_id, ch_id, thread_id=None):
        deleted.append((msg_id, thread_id))

    monkeypatch.setattr(bot_module, "_delete_webhook_message", fake_delete)

    run(bot_module.on_raw_message_delete(SimpleNamespace(message_id=9001, channel_id=6101)))

    assert sorted(deleted) == [(7202, 6202), (7203, 6203)]


def test_non_thread_cluster_passes_no_thread():
    assert bot_module._thread_kwargs(bot_module._thread_of({"channels": {}}, 202)) == {}
    assert bot_module._thread_kwargs(bot_module._thread_of({"thread_channels": {}}, 202)) == {}
