import json
from unittest.mock import Mock

import translator


def _read_log(path):
    """Parse the JSON-lines log, skipping any unparseable line."""
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return entries


def test_log_event_written_to_json_file(tmp_path, monkeypatch, capsys):
    log_file = tmp_path / "bot_log.jsonl"
    monkeypatch.setattr(translator, "LOG_FILE", str(log_file))

    translator.log_event("Failed to delete webhook message 123 in channel 456: 404")

    entries = _read_log(log_file)
    assert len(entries) == 1
    assert entries[0]["message"] == "Failed to delete webhook message 123 in channel 456: 404"
    assert entries[0]["type"] == "info"
    assert "time" in entries[0]

    # Still prints to console for real-time DSM log viewing.
    captured = capsys.readouterr()
    assert "Failed to delete webhook message 123 in channel 456: 404" in captured.out


def test_log_event_accepts_extra_fields(tmp_path, monkeypatch):
    log_file = tmp_path / "bot_log.jsonl"
    monkeypatch.setattr(translator, "LOG_FILE", str(log_file))

    translator.log_event("something happened", type="thread", channel_id=456)

    entries = _read_log(log_file)
    assert entries[0]["type"] == "thread"
    assert entries[0]["channel_id"] == 456


def test_log_event_and_translate_event_share_the_same_file(tmp_path, monkeypatch):
    log_file = tmp_path / "bot_log.jsonl"
    monkeypatch.setattr(translator, "LOG_FILE", str(log_file))
    chain = Mock()
    chain.translate.return_value = "你好"
    monkeypatch.setattr(translator, "_get_provider_chain", lambda: chain)

    translator.log_event("bot started")
    translator._translate_with_fallback("hello", "en", "zh-TW")

    entries = _read_log(log_file)
    assert len(entries) == 2
    assert entries[0]["type"] == "info"
    assert entries[1]["type"] == "translate"


def test_log_event_appends_without_rewriting_existing_lines(tmp_path, monkeypatch):
    log_file = tmp_path / "bot_log.jsonl"
    monkeypatch.setattr(translator, "LOG_FILE", str(log_file))

    translator.log_event("first")
    first_bytes = log_file.read_bytes()
    translator.log_event("second")

    assert log_file.read_bytes().startswith(first_bytes)
    assert [e["message"] for e in _read_log(log_file)] == ["first", "second"]


def test_log_event_recovers_from_unterminated_last_line(tmp_path, monkeypatch):
    log_file = tmp_path / "bot_log.jsonl"
    log_file.write_text('{"message": "ok"}\n{"message": "cut off by a cra')
    monkeypatch.setattr(translator, "LOG_FILE", str(log_file))

    translator.log_event("after crash")

    entries = _read_log(log_file)
    assert [e["message"] for e in entries] == ["ok", "after crash"]


def test_log_trim_keeps_newest_entries_and_leaves_no_temp_file(tmp_path, monkeypatch):
    log_file = tmp_path / "bot_log.jsonl"
    monkeypatch.setattr(translator, "LOG_FILE", str(log_file))
    monkeypatch.setattr(translator, "LOG_MAX_ENTRIES", 10)

    for i in range(40):
        translator.log_event(f"event-{i}")

    entries = _read_log(log_file)
    assert 10 <= len(entries) <= 11  # trimmed at cap + 10%
    assert entries[-1]["message"] == "event-39"
    assert [p.name for p in tmp_path.iterdir()] == ["bot_log.jsonl"]


def test_legacy_json_array_log_is_moved_aside_not_mixed(tmp_path, monkeypatch):
    log_file = tmp_path / "bot_log.json"
    legacy = '[{"message": "old"}]'
    log_file.write_text(legacy)
    monkeypatch.setattr(translator, "LOG_FILE", str(log_file))

    translator.log_event("new")

    assert (tmp_path / "bot_log.json.legacy").read_text() == legacy
    assert [e["message"] for e in _read_log(log_file)] == ["new"]
