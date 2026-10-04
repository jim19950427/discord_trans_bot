import json
import os

import pytest

import config
import glossary


def test_atomic_write_json_round_trips_and_leaves_no_temp(tmp_path):
    target = tmp_path / "data.json"
    assert config.atomic_write_json(str(target), {"名稱": [1, 2]}, indent=2)
    assert json.loads(target.read_text(encoding="utf-8")) == {"名稱": [1, 2]}
    assert [p.name for p in tmp_path.iterdir()] == ["data.json"]


def test_failed_write_keeps_old_file_intact(tmp_path, monkeypatch):
    target = tmp_path / "data.json"
    target.write_text('{"old": true}')

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        config.atomic_write_json(str(target), {"new": True})

    assert json.loads(target.read_text()) == {"old": True}
    assert [p.name for p in tmp_path.iterdir()] == ["data.json"]


def test_unserializable_data_never_touches_existing_file(tmp_path):
    target = tmp_path / "data.json"
    target.write_text('{"old": true}')

    with pytest.raises(TypeError):
        config.atomic_write_json(str(target), {"bad": object()})

    assert json.loads(target.read_text()) == {"old": True}


def test_skip_if_unchanged_only_writes_on_change(tmp_path):
    target = tmp_path / "data.json"
    assert config.atomic_write_json(str(target), {"a": 1}, skip_if_unchanged=True)
    mtime = target.stat().st_mtime_ns
    assert not config.atomic_write_json(str(target), {"a": 1}, skip_if_unchanged=True)
    assert target.stat().st_mtime_ns == mtime
    assert config.atomic_write_json(str(target), {"a": 2}, skip_if_unchanged=True)
    assert json.loads(target.read_text()) == {"a": 2}


def test_skip_if_unchanged_rewrites_a_deleted_file(tmp_path):
    target = tmp_path / "data.json"
    config.atomic_write_json(str(target), {"a": 1}, skip_if_unchanged=True)
    target.unlink()
    assert config.atomic_write_json(str(target), {"a": 1}, skip_if_unchanged=True)
    assert target.exists()


def test_corrupt_clusters_file_is_quarantined_not_overwritten(tmp_path, monkeypatch):
    clusters_file = tmp_path / "msg_clusters.json"
    clusters_file.write_text('{"123": {"channels": ')
    monkeypatch.setattr(glossary, "CLUSTERS_FILE", str(clusters_file))

    assert glossary.load_clusters() == {}

    assert not clusters_file.exists()
    saved = [p for p in tmp_path.iterdir() if ".corrupt-" in p.name]
    assert len(saved) == 1
    assert saved[0].read_text() == '{"123": {"channels": '


def test_glossary_saves_are_atomic_json(tmp_path, monkeypatch):
    monkeypatch.setattr(glossary, "GLOSSARY_FILE", str(tmp_path / "glossary.json"))
    glossary.save_glossary({"1": {"term": {"en": "x"}}})
    assert glossary.load_glossary() == {"1": {"term": {"en": "x"}}}
    assert [p.name for p in tmp_path.iterdir()] == ["glossary.json"]


def test_channel_pins_round_trip_sorted(tmp_path, monkeypatch):
    monkeypatch.setattr(glossary, "CHANNEL_PINS_FILE", str(tmp_path / "pins.json"))
    glossary.save_channel_pins({5: {30, 10, 20}})
    assert glossary.load_channel_pins() == {5: {10, 20, 30}}


def test_cleanup_stale_tmp_removes_only_orphaned_temp_files(tmp_path):
    data = tmp_path / "msg_clusters.json"
    data.write_text("{}")
    orphan = tmp_path / "msg_clusters.json.123.456.tmp"
    orphan.write_text("half")
    unrelated = tmp_path / "notes.tmp"
    unrelated.write_text("keep")
    other_data = tmp_path / "glossary.json.9.9.tmp"
    other_data.write_text("not in the list")

    assert config.cleanup_stale_tmp([str(data)]) == 1

    assert not orphan.exists()
    assert data.exists() and unrelated.exists() and other_data.exists()


def test_cleanup_stale_tmp_tolerates_missing_directory(tmp_path):
    assert config.cleanup_stale_tmp([str(tmp_path / "nope" / "x.json")]) == 0
