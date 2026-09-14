import json
from unittest.mock import Mock

import translator


def test_log_event_written_to_json_file(tmp_path, monkeypatch, capsys):
    log_file = tmp_path / "bot_log.json"
    monkeypatch.setattr(translator, "LOG_FILE", str(log_file))

    translator.log_event("Failed to delete webhook message 123 in channel 456: 404")

    entries = json.loads(log_file.read_text())
    assert len(entries) == 1
    assert entries[0]["message"] == "Failed to delete webhook message 123 in channel 456: 404"
    assert entries[0]["type"] == "info"
    assert "time" in entries[0]

    # Still prints to console for real-time DSM log viewing.
    captured = capsys.readouterr()
    assert "Failed to delete webhook message 123 in channel 456: 404" in captured.out


def test_log_event_accepts_extra_fields(tmp_path, monkeypatch):
    log_file = tmp_path / "bot_log.json"
    monkeypatch.setattr(translator, "LOG_FILE", str(log_file))

    translator.log_event("something happened", type="thread", channel_id=456)

    entries = json.loads(log_file.read_text())
    assert entries[0]["type"] == "thread"
    assert entries[0]["channel_id"] == 456


def test_log_event_and_translate_event_share_the_same_file(tmp_path, monkeypatch):
    log_file = tmp_path / "bot_log.json"
    monkeypatch.setattr(translator, "LOG_FILE", str(log_file))
    chain = Mock()
    chain.translate.return_value = "你好"
    monkeypatch.setattr(translator, "_get_provider_chain", lambda: chain)

    translator.log_event("bot started")
    translator._translate_with_fallback("hello", "en", "zh-TW")

    entries = json.loads(log_file.read_text())
    assert len(entries) == 2
    assert entries[0]["type"] == "info"
    assert entries[1]["type"] == "translate"
