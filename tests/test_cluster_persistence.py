import json

import pytest

import glossary


@pytest.mark.parametrize("raw_forward", [True, False])
def test_cluster_round_trip_preserves_raw_forward_mode(tmp_path, monkeypatch, raw_forward):
    """A restart must not switch a raw-forward missing-channel retry to translation."""
    cluster_file = tmp_path / "clusters.json"
    monkeypatch.setattr(glossary, "CLUSTERS_FILE", str(cluster_file))
    cluster = {
        "channels": {101: 9001},
        "contents": {101: "Keep this unchanged"},
        "author": "Tester",
        "avatar_url": "",
        "source_ch": 101,
        "source_lang": "auto",
        "raw_forward": raw_forward,
        "att_names": {101: ["image.png"]},
        "att_urls": {101: ["https://example.invalid/image.png"]},
    }

    glossary.save_clusters({9001: cluster})
    restored = glossary.load_clusters()

    assert restored[9001]["raw_forward"] is raw_forward
    assert restored == {9001: cluster}


def test_historical_cluster_defaults_to_normal_translation(tmp_path, monkeypatch):
    """Old records have no mode flag; their content cannot safely imply raw forwarding."""
    cluster_file = tmp_path / "clusters.json"
    monkeypatch.setattr(glossary, "CLUSTERS_FILE", str(cluster_file))
    cluster_file.write_text(json.dumps({
        "9001": {
            "channels": {"101": 9001},
            "contents": {"101": "Historical content"},
            "source_ch": 101,
            "source_lang": "zh-TW",
        }
    }))

    restored = glossary.load_clusters()

    assert restored[9001]["raw_forward"] is False
    assert restored[9001]["contents"] == {101: "Historical content"}


def _cluster(source_ch, ids, text="hi"):
    return {
        "channels": {101 + i: msg_id for i, msg_id in enumerate(ids)},
        "contents": {101 + i: text for i in range(len(ids))},
        "author": "Tester",
        "avatar_url": "",
        "source_ch": source_ch,
        "source_lang": "auto",
        "raw_forward": False,
    }


def test_restored_ids_of_one_message_share_a_single_cluster(tmp_path, monkeypatch):
    """After a restart, an edit through one copy must be visible from every copy."""
    monkeypatch.setattr(glossary, "CLUSTERS_FILE", str(tmp_path / "clusters.json"))
    cluster = _cluster(101, [9001, 9002, 9003])
    other = _cluster(101, [9101, 9102])
    glossary.save_clusters({9001: cluster, 9002: cluster, 9003: cluster, 9101: other, 9102: other})

    restored = glossary.load_clusters()

    assert restored[9001] is restored[9002] is restored[9003]
    assert restored[9101] is restored[9102]
    assert restored[9001] is not restored[9101]
    restored[9002]["contents"][101] = "edited"
    assert restored[9003]["contents"][101] == "edited"


def test_saved_file_stores_each_cluster_once(tmp_path, monkeypatch):
    path = tmp_path / "clusters.json"
    monkeypatch.setattr(glossary, "CLUSTERS_FILE", str(path))
    cluster = _cluster(101, list(range(9001, 9010)))
    glossary.save_clusters({k: cluster for k in range(9001, 9010)})

    raw = json.loads(path.read_text())

    assert raw["version"] == 2
    assert len(raw["clusters"]) == 1
    assert raw["clusters"][0]["keys"] == list(range(9001, 9010))


def test_partially_evicted_cluster_keeps_only_its_live_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(glossary, "CLUSTERS_FILE", str(tmp_path / "clusters.json"))
    cluster = _cluster(101, [9001, 9002, 9003])
    glossary.save_clusters({9002: cluster, 9003: cluster})

    restored = glossary.load_clusters()

    assert sorted(restored) == [9002, 9003]
    assert restored[9002] is restored[9003]


def test_legacy_per_id_copies_are_reshared_on_load(tmp_path, monkeypatch):
    """The pre-v2 file wrote a full copy per id; identical copies were one dict."""
    path = tmp_path / "clusters.json"
    monkeypatch.setattr(glossary, "CLUSTERS_FILE", str(path))
    entry = {
        "channels": {"101": 9001, "102": 9002},
        "contents": {"101": "a", "102": "b"},
        "source_ch": 101,
        "source_lang": "auto",
    }
    diverged = dict(entry, contents={"101": "a", "102": "stale"})
    path.write_text(json.dumps({"9001": entry, "9002": entry, "9003": diverged}))

    restored = glossary.load_clusters()

    assert restored[9001] is restored[9002]
    assert restored[9003] is not restored[9001]
    assert restored[9003]["contents"][102] == "stale"


def test_unsupported_cluster_format_is_quarantined(tmp_path, monkeypatch):
    path = tmp_path / "clusters.json"
    path.write_text(json.dumps({"version": 99, "clusters": []}))
    monkeypatch.setattr(glossary, "CLUSTERS_FILE", str(path))

    assert glossary.load_clusters() == {}

    assert not path.exists()
    assert len([p for p in tmp_path.iterdir() if ".corrupt-" in p.name]) == 1
