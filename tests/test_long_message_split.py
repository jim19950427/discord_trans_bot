import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot as bot_module
import glossary


def run(coro):
    return asyncio.run(coro)


# ---- _split_message ------------------------------------------------------

def test_short_content_is_not_split():
    assert bot_module._split_message("hello") == ["hello"]
    assert bot_module._split_message("x" * 2000) == ["x" * 2000]


def test_long_content_splits_into_parts_within_the_limit_without_losing_text():
    content = "\n".join(f"line {i} " + "word " * 20 for i in range(60))

    parts = bot_module._split_message(content)

    assert len(parts) > 1
    assert all(0 < bot_module._discord_len(p) <= 2000 for p in parts)
    assert "\n".join(parts).replace("\n", " ").split() == content.replace("\n", " ").split()


def test_split_prefers_a_line_boundary():
    content = ("a" * 1500) + "\n" + ("b" * 1500)

    parts = bot_module._split_message(content)

    assert parts == ["a" * 1500, "b" * 1500]


def test_single_unbroken_run_is_cut_hard_and_loses_nothing():
    content = "x" * 4500

    parts = bot_module._split_message(content)

    assert all(len(p) <= 2000 for p in parts)
    assert "".join(parts) == content


def test_astral_characters_are_budgeted_as_two_units():
    content = "😀" * 1500  # 3000 units by Discord's count

    parts = bot_module._split_message(content)

    assert len(parts) >= 2
    assert all(bot_module._discord_len(p) <= 2000 for p in parts)
    assert "".join(parts) == content


def test_code_block_is_closed_and_reopened_across_parts():
    content = "intro\n```\n" + "\n".join(f"code line {i}" for i in range(400)) + "\n```\noutro"

    parts = bot_module._split_message(content)

    assert len(parts) > 1
    for part in parts:
        assert part.count("```") % 2 == 0, "every part must have balanced fences"
        assert bot_module._discord_len(part) <= 2000
    assert parts[1].startswith("```\n")


# ---- sending ---------------------------------------------------------------

class _FakeSender:
    def __init__(self, fail_on_call=None):
        self.calls = []
        self.fail_on_call = fail_on_call
        self._next = 5000

    async def __call__(self, url, kwargs, files):
        self.calls.append((url, dict(kwargs), list(files)))
        if self.fail_on_call == len(self.calls):
            raise RuntimeError("boom")
        self._next += 1
        return SimpleNamespace(id=self._next)


@pytest.fixture
def sender(monkeypatch):
    fake = _FakeSender()
    monkeypatch.setattr(bot_module, "_webhook_send", fake)
    monkeypatch.setattr(bot_module, "log_event", lambda *a, **k: None)
    return fake


def test_send_with_parts_puts_files_on_the_first_part_only(sender):
    content = ("a" * 1500) + "\n" + ("b" * 1500)
    kwargs = {"username": "Ann", "avatar_url": "av", "wait": True, "content": content,
              "thread": SimpleNamespace(id=9)}

    first, extras = run(bot_module._send_with_parts("hook", kwargs, [("f.png", b"x")]))

    assert first.id == 5001 and extras == [5002]
    (_, k1, f1), (_, k2, f2) = sender.calls
    assert f1 == [("f.png", b"x")] and f2 == []
    assert k1["content"] == "a" * 1500 and k2["content"] == "b" * 1500
    assert k2["username"] == "Ann" and k2["thread"] is kwargs["thread"]


def test_short_content_is_one_plain_send(sender):
    first, extras = run(bot_module._send_with_parts(
        "hook", {"username": "Ann", "wait": True, "content": "hi"}, []))

    assert extras == [] and len(sender.calls) == 1


def test_content_less_send_with_only_files_still_works(sender):
    first, extras = run(bot_module._send_with_parts(
        "hook", {"username": "Ann", "wait": True}, [("f.png", b"x")]))

    assert extras == [] and sender.calls[0][2] == [("f.png", b"x")]


def test_failed_continuation_keeps_the_parts_already_sent(monkeypatch):
    fake = _FakeSender(fail_on_call=3)
    monkeypatch.setattr(bot_module, "_webhook_send", fake)
    monkeypatch.setattr(bot_module, "log_event", lambda *a, **k: None)
    content = "\n".join(["z" * 1500] * 3)

    first, extras = run(bot_module._send_with_parts("hook", {"username": "A", "content": content}, []))

    assert first.id == 5001 and extras == [5002]


def test_send_pretranslated_returns_continuation_ids(sender):
    outcome = bot_module.TranslationOutcome("\n".join(["t" * 1500] * 2), True)

    result = run(bot_module._send_pretranslated(outcome, "ru", "hook", "Ann", "", [], [], None))

    assert result.message_id == 5001 and result.extra_ids == (5002,)
    assert tuple(result) == (5001, outcome.text)  # legacy 2-tuple unpacking still works


# ---- cluster bookkeeping ---------------------------------------------------

def _cluster():
    return {"channels": {101: 1, 202: 20}, "contents": {101: "src", 202: "t"},
            "author": "Ann", "avatar_url": "av", "source_ch": 101, "source_lang": "auto"}


def test_record_forward_tracks_extras_and_clears_them_when_gone():
    cluster = _cluster()
    result = bot_module._ForwardResult(21, "text", True, (22, 23))

    assert bot_module._record_forward(cluster, 202, result) == "text"
    assert cluster["channels"][202] == 21 and cluster["extra_parts"] == {202: [22, 23]}
    assert set(bot_module._cluster_messages(cluster)) == {(101, 1), (202, 21), (202, 22), (202, 23)}

    bot_module._record_forward(cluster, 202, bot_module._ForwardResult(30, "x", True))
    assert "extra_parts" not in cluster


def test_record_forward_accepts_a_plain_tuple_result():
    cluster = _cluster()
    bot_module._record_forward(cluster, 202, (40, "hello"))
    assert cluster["channels"][202] == 40 and "extra_parts" not in cluster


def test_store_and_evict_cover_continuation_ids(monkeypatch):
    monkeypatch.setattr(bot_module, "_msg_clusters", {})
    monkeypatch.setattr(bot_module, "_MAX_CLUSTER_ENTRIES", 6)
    old = _cluster()
    old["extra_parts"] = {202: [21, 22]}
    bot_module._store_cluster(old)
    assert set(bot_module._msg_clusters) == {1, 20, 21, 22}

    newer = {"channels": {101: 100, 202: 200, 303: 300}, "contents": {}, "author": "", "avatar_url": "",
             "source_ch": 101, "source_lang": "auto"}
    bot_module._store_cluster(newer)  # 7 ids > 6 -> evict the old cluster entirely

    assert set(bot_module._msg_clusters) == {100, 200, 300}


def test_extra_parts_survive_a_save_and_load(tmp_path, monkeypatch):
    monkeypatch.setattr(glossary, "CLUSTERS_FILE", str(tmp_path / "c.json"))
    cluster = _cluster()
    cluster["extra_parts"] = {202: [21, 22]}
    glossary.save_clusters({1: cluster, 20: cluster, 21: cluster, 22: cluster})

    restored = glossary.load_clusters()

    assert restored[22]["extra_parts"] == {202: [21, 22]}
    assert restored[1] is restored[22]


# ---- editing ----------------------------------------------------------------

@pytest.fixture
def edit_env(monkeypatch):
    edits, deletes = [], []

    class Webhook:
        async def edit_message(self, msg_id, **kwargs):
            edits.append((msg_id, kwargs["content"]))

    monkeypatch.setattr(bot_module.discord.Webhook, "from_url", lambda url, **k: Webhook())
    monkeypatch.setattr(bot_module, "_msg_clusters", {})
    monkeypatch.setattr(bot_module, "log_event", lambda *a, **k: None)
    sender = _FakeSender()
    monkeypatch.setattr(bot_module, "_webhook_send", sender)

    async def fake_delete(url, msg_id, ch_id):
        deletes.append(msg_id)

    monkeypatch.setattr(bot_module, "_delete_webhook_message", fake_delete)
    return SimpleNamespace(edits=edits, deletes=deletes, sender=sender)


LONG = "\n".join(["w" * 1500] * 3)   # needs 3 parts


def test_edit_that_grows_adds_continuation_messages(edit_env):
    cluster = _cluster()

    run(bot_module._edit_message_parts("hook", 20, 202, cluster, LONG))

    assert [m for m, _ in edit_env.edits] == [20]
    assert cluster["extra_parts"] == {202: [5001, 5002]}
    assert bot_module._msg_clusters[5001] is cluster and bot_module._msg_clusters[5002] is cluster
    assert edit_env.sender.calls[0][1]["username"] == "Ann"


def test_edit_that_shrinks_deletes_surplus_parts(edit_env):
    cluster = _cluster()
    cluster["extra_parts"] = {202: [21, 22]}
    bot_module._msg_clusters.update({21: cluster, 22: cluster})

    run(bot_module._edit_message_parts("hook", 20, 202, cluster, "short"))

    assert edit_env.edits == [(20, "short")]
    assert edit_env.deletes == [21, 22]
    assert "extra_parts" not in cluster
    assert 21 not in bot_module._msg_clusters and 22 not in bot_module._msg_clusters


def test_edit_with_the_same_part_count_edits_each_part(edit_env):
    cluster = _cluster()
    cluster["extra_parts"] = {202: [21, 22]}

    run(bot_module._edit_message_parts("hook", 20, 202, cluster, LONG))

    assert [m for m, _ in edit_env.edits] == [20, 21, 22]
    assert edit_env.deletes == [] and cluster["extra_parts"] == {202: [21, 22]}


def test_failed_growth_keeps_extra_parts_consistent(edit_env, monkeypatch):
    cluster = _cluster()
    monkeypatch.setattr(bot_module, "_webhook_send", _FakeSender(fail_on_call=2))

    with pytest.raises(RuntimeError):
        run(bot_module._edit_message_parts("hook", 20, 202, cluster, LONG))

    assert cluster["extra_parts"] == {202: [5001]}  # only what really exists


def test_edit_pretranslated_uses_the_parts_editor(edit_env):
    cluster = _cluster()
    cluster["prefixes"] = {202: "> quoted"}

    translated = run(bot_module._edit_pretranslated(
        bot_module.TranslationOutcome("new text", True), "hook", 20, 202, cluster))

    assert translated == "new text" and edit_env.edits == [(20, "> quoted\nnew text")]


# ---- deleting ---------------------------------------------------------------

def test_deleting_a_message_removes_every_continuation_part(monkeypatch):
    deleted = []
    cluster = _cluster()
    cluster["extra_parts"] = {202: [21, 22]}
    monkeypatch.setattr(bot_module, "_msg_clusters", {1: cluster, 20: cluster, 21: cluster, 22: cluster})
    monkeypatch.setattr(bot_module, "_guild_channels_for",
                        lambda ch: {202: {"webhook_url": "hook"}})

    async def fake_delete(url, msg_id, ch_id):
        deleted.append(msg_id)

    monkeypatch.setattr(bot_module, "_delete_webhook_message", fake_delete)

    run(bot_module.on_raw_message_delete(SimpleNamespace(message_id=1, channel_id=101)))

    assert sorted(deleted) == [20, 21, 22]
    assert bot_module._msg_clusters == {}


# ---- end to end through on_message ----------------------------------------

def test_on_message_with_a_translation_over_the_limit_posts_parts_and_tracks_them(monkeypatch):
    import translator

    long_translation = "\n".join(["ж" * 1400] * 3)
    monkeypatch.setattr(bot_module.bot, "process_commands", AsyncMock())
    monkeypatch.setattr(bot_module, "_msg_clusters", {})
    monkeypatch.setattr(bot_module, "_thread_clusters", {})
    monkeypatch.setattr(bot_module, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(bot_module, "get_guild_glossary", lambda *a: {})
    monkeypatch.setattr(bot_module, "get_guild_substitutions", lambda *a: {})
    monkeypatch.setattr(
        bot_module, "translate_many_with_status",
        lambda *a, **k: {"ru": translator.TranslationOutcome(long_translation, True)},
    )
    sender = _FakeSender()
    monkeypatch.setattr(bot_module, "_webhook_send", sender)
    monkeypatch.setattr(bot_module, "channel_configs", {7: {
        101: {"lang": "zh-TW", "webhook_url": "src", "group": "default"},
        202: {"lang": "ru", "webhook_url": "ru-hook", "group": "default"},
    }})
    message = SimpleNamespace(
        author=SimpleNamespace(bot=False, display_name="Ann",
                               display_avatar=SimpleNamespace(url="av")),
        channel=SimpleNamespace(id=101), guild=SimpleNamespace(id=7),
        content="long source text", attachments=[], stickers=[], reference=None,
        embeds=[], id=9001,
    )

    run(bot_module.on_message(message))

    assert len(sender.calls) == 3
    assert all(bot_module._discord_len(k["content"]) <= 2000 for _, k, _ in sender.calls)
    cluster = bot_module._msg_clusters[9001]
    assert cluster["channels"][202] == 5001
    assert cluster["extra_parts"] == {202: [5002, 5003]}
    assert cluster["contents"][202] == long_translation   # full text kept for later edits
    # every part resolves to the same cluster, so deleting/replying to any part works
    assert all(bot_module._msg_clusters[i] is cluster for i in (9001, 5001, 5002, 5003))


# ---- review fixes ------------------------------------------------------------

def test_emoji_heavy_split_does_not_drop_characters():
    # Newlines fall inside the budget, but the astral width pushes the part
    # over it, so the cut backs off the separator; nothing may be lost.
    line = "😀" * 400 + "\n"
    content = line * 6

    parts = bot_module._split_message(content)

    assert all(bot_module._discord_len(p) <= 2000 for p in parts)
    assert "".join(parts).replace("\n", "") == content.replace("\n", "")


def test_pending_retry_remembers_continuation_ids(monkeypatch):
    saved = []
    monkeypatch.setattr(bot_module, "_pending_retries", {})
    monkeypatch.setattr(bot_module, "_persist_pending_retries", AsyncMock())
    monkeypatch.setattr(bot_module, "_spawn", lambda coro: coro.close())

    run(bot_module._schedule_retry("txt", "auto", "ru", 7, 202, 20, extra_ids=(21, 22)))

    assert bot_module._pending_retries["202:20"]["extra_ids"] == [21, 22]


def test_restored_retry_edits_the_continuation_messages_too(monkeypatch):
    seen = {}

    async def fake_retry_translate(text, src, dest, url, msg_id, ch_id, cluster, glossary=None, delay=60):
        seen["extra_parts"] = cluster.get("extra_parts")

    monkeypatch.setattr(bot_module, "_msg_clusters", {})
    monkeypatch.setattr(bot_module, "_pending_retries", {"202:20": {
        "text": "t", "src": "auto", "dest": "ru", "guild_id": 7, "ch_id": 202, "msg_id": 20,
        "due": 0, "prefix": "", "extra_ids": [21, 22]}})
    monkeypatch.setattr(bot_module, "_persist_pending_retries", AsyncMock())
    monkeypatch.setattr(bot_module, "_retry_translate", fake_retry_translate)
    monkeypatch.setattr(bot_module, "channel_configs", {7: {202: {"webhook_url": "hook"}}})
    monkeypatch.setattr(bot_module, "get_guild_glossary", lambda *a: {})

    run(bot_module._run_retry("202:20"))

    assert seen["extra_parts"] == {202: [21, 22]}


def test_pending_retry_file_accepts_extra_ids_and_rejects_bad_ones(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(glossary, "PENDING_RETRIES_FILE", str(tmp_path / "p.json"))
    base = {"text": "t", "src": "a", "dest": "b", "guild_id": 1, "ch_id": 2, "msg_id": 3, "due": 1.0}
    (tmp_path / "p.json").write_text(json.dumps([
        {**base, "extra_ids": [4, 5]},
        {**base, "msg_id": 6, "extra_ids": ["x"]},
        {**base, "msg_id": 7},            # entries written before this feature
    ]))

    kept = [e["msg_id"] for e in glossary.load_pending_retries()]

    assert kept == [3, 7]
