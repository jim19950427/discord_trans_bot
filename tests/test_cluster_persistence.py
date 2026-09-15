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
