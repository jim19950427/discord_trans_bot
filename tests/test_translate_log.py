import json
from unittest.mock import Mock

import translator


def _install_chain(monkeypatch, result):
    chain = Mock()
    chain.translate.side_effect = result if callable(result) else None
    if not callable(result):
        chain.translate.return_value = result
    monkeypatch.setattr(translator, "_get_provider_chain", lambda: chain)
    return chain


def test_translate_event_logged_to_json_file(tmp_path, monkeypatch):
    log_file = tmp_path / "translate_log.json"
    monkeypatch.setattr(translator, "LOG_FILE", str(log_file))
    _install_chain(monkeypatch, "你好")

    result = translator._translate_with_fallback("hello", "en", "zh-TW")

    assert result == "你好"
    entries = json.loads(log_file.read_text())
    assert len(entries) == 1
    assert entries[0]["input"] == "hello"
    assert entries[0]["output"] == "你好"
    assert entries[0]["src"] == "en"
    assert entries[0]["dest"] == "zh-TW"
    assert entries[0]["type"] == "translate"
    assert "time" in entries[0]


def test_failed_translation_also_logged(tmp_path, monkeypatch):
    log_file = tmp_path / "translate_log.json"
    monkeypatch.setattr(translator, "LOG_FILE", str(log_file))
    _install_chain(monkeypatch, None)

    result = translator._translate_with_fallback("hello", "en", "zh-TW")

    assert result is None
    entries = json.loads(log_file.read_text())
    assert len(entries) == 1
    assert entries[0]["output"] is None
    assert entries[0]["type"] == "translate"


def test_translate_log_capped_at_max_entries(tmp_path, monkeypatch):
    log_file = tmp_path / "translate_log.json"
    monkeypatch.setattr(translator, "LOG_FILE", str(log_file))
    monkeypatch.setattr(translator, "LOG_MAX_ENTRIES", 3)
    _install_chain(monkeypatch, lambda t, s, d: f"out-{t}")

    for i in range(5):
        translator._translate_with_fallback(f"in-{i}", "en", "zh-TW")

    entries = json.loads(log_file.read_text())
    assert len(entries) == 3
    assert [e["input"] for e in entries] == ["in-2", "in-3", "in-4"]


def test_translate_log_handles_corrupted_existing_file(tmp_path, monkeypatch):
    log_file = tmp_path / "translate_log.json"
    log_file.write_text("not valid json")
    monkeypatch.setattr(translator, "LOG_FILE", str(log_file))
    _install_chain(monkeypatch, "ok")

    translator._translate_with_fallback("hello", "en", "zh-TW")

    entries = json.loads(log_file.read_text())
    assert len(entries) == 1
    assert entries[0]["output"] == "ok"


def test_provider_chain_receives_normalized_codes(tmp_path, monkeypatch):
    monkeypatch.setattr(translator, "LOG_FILE", str(tmp_path / "log.json"))
    chain = _install_chain(monkeypatch, "hello")

    assert translator._translate_with_fallback("你好", "zh-tw", "EN") == "hello"
    chain.translate.assert_called_once_with("你好", "zh-TW", "en")
