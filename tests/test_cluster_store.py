import asyncio

import bot as bot_module


def _store(next_id, size=3):
    ids = list(range(next_id, next_id + size))
    cluster = {
        "channels": {101 + i: msg_id for i, msg_id in enumerate(ids)},
        "contents": {}, "author": "t", "avatar_url": "", "source_ch": 101, "source_lang": "auto",
    }
    bot_module._store_cluster(cluster)
    return cluster


def test_eviction_drops_whole_clusters_oldest_first(monkeypatch):
    monkeypatch.setattr(bot_module, "_msg_clusters", {})
    monkeypatch.setattr(bot_module, "_MAX_CLUSTER_ENTRIES", 30)
    clusters = [_store(1000 + i * 10) for i in range(11)]  # 33 ids > 30 -> evict

    store = bot_module._msg_clusters
    assert len(store) <= 27
    # newest cluster always survives, oldest is gone entirely (no half-tracked leftovers)
    assert all(i in store for i in clusters[-1]["channels"].values())
    assert not any(i in store for i in clusters[0]["channels"].values())
    for key, cluster in store.items():
        assert all(store.get(i) is cluster for i in cluster["channels"].values())


def test_eviction_never_drops_the_cluster_being_stored(monkeypatch):
    monkeypatch.setattr(bot_module, "_msg_clusters", {})
    monkeypatch.setattr(bot_module, "_MAX_CLUSTER_ENTRIES", 2)
    cluster = _store(5000, size=3)

    assert set(bot_module._msg_clusters) == {5000, 5001, 5002}
    assert bot_module._msg_clusters[5000] is cluster


def test_persist_loop_survives_a_failing_write_and_logs_it(monkeypatch):
    """An unhandled exception would stop the tasks.loop for good."""
    logged, written = [], []
    monkeypatch.setattr(bot_module, "log_event", lambda msg, **kw: logged.append((msg, kw)))

    def boom(_):
        raise RuntimeError("disk full")

    monkeypatch.setattr(bot_module, "write_clusters", boom)
    monkeypatch.setattr(bot_module, "write_thread_clusters", lambda data: written.append("threads"))
    monkeypatch.setattr(bot_module, "write_channel_pins", lambda data: written.append("pins"))

    asyncio.run(bot_module._persist_clusters.coro())

    assert written == ["threads", "pins"]
    assert len(logged) == 1
    assert logged[0][1]["type"] == "error" and "msg clusters" in logged[0][0]
