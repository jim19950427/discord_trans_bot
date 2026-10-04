from unittest.mock import Mock

import diskcache
import pytest

import translator


def test_batch_sends_only_uncached_targets(tmp_path, monkeypatch):
    calls = []

    class Chain:
        def translate_many(self, text, source, targets):
            calls.append((text, source, list(targets)))
            return {"ja": "こんにちは"}

    with diskcache.Cache(str(tmp_path / "cache")) as cache:
        cache[("你好", "auto", "en")] = "Hello"
        monkeypatch.setattr(translator, "_get_translate_cache", lambda: cache)
        monkeypatch.setattr(translator, "_get_provider_chain", lambda: Chain())
        result = translator.translate_many_with_status("你好", ["en", "ja"])
        assert result["en"] == translator.TranslationOutcome("Hello", True)
        assert result["ja"] == translator.TranslationOutcome("こんにちは", True)
        assert cache[("你好", "auto", "ja")] == "こんにちは"
    assert calls == [("你好", "auto", ["ja"])]


def test_batch_cache_stores_raw_placeholders_and_only_nonempty_successes(tmp_path, monkeypatch):
    calls = []

    class Chain:
        def translate_many(self, text, source, targets):
            calls.append(list(targets))
            return {"en": "Hello §0§", "ja": "", "de": None}

    with diskcache.Cache(str(tmp_path / "cache")) as cache:
        monkeypatch.setattr(translator, "_get_translate_cache", lambda: cache)
        monkeypatch.setattr(translator, "_get_provider_chain", lambda: Chain())
        first = translator.translate_many_with_status(
            "Hi Jim", ["en", "ja", "de"], glossary={"Jim": {"*": "James"}}
        )
        second = translator.translate_many_with_status(
            "Hi Jim", ["en", "ja", "de"], glossary={"Jim": {"*": "Jimmy"}}
        )
        assert first["en"].text == "Hello James"
        assert second["en"].text == "Hello Jimmy"
        assert first["ja"].provider_succeeded is False
        assert first["de"].provider_succeeded is False
        assert list(cache) == [("Hi §0§", "auto", "en")]
        assert cache[("Hi §0§", "auto", "en")] == "Hello §0§"
    assert calls == [["en", "ja", "de"], ["ja", "de"]]


def test_single_target_preserves_explicit_source_and_uses_shared_cache(tmp_path, monkeypatch):
    calls = []

    class Chain:
        def translate_many(self, text, source, targets):
            calls.append((text, source, list(targets)))
            return {"en": "Hello"}

    with diskcache.Cache(str(tmp_path / "cache")) as cache:
        monkeypatch.setattr(translator, "_get_translate_cache", lambda: cache)
        monkeypatch.setattr(translator, "_get_provider_chain", lambda: Chain())
        assert translator.translate_text("你好", "zh-tw", "EN") == "Hello"
        assert cache[("你好", "zh-TW", "en")] == "Hello"
        result = translator.translate_text_with_status("你好", "zh-TW", "en")
        assert result == translator.TranslationOutcome("Hello", True)
        assert translator.translate_text("Hello", "en", "en") is None
    assert calls == [("你好", "zh-TW", ["en"])]


def test_cache_persists_across_restart(tmp_path, monkeypatch):
    cache_dir = str(tmp_path / "cache")
    mock_translate = Mock(return_value="你好")
    monkeypatch.setattr(translator, "_translate_with_fallback", mock_translate)

    cache1 = diskcache.Cache(cache_dir)
    result1 = translator._cached_translate("hello", "en", "zh-TW", cache=cache1)
    assert result1 == "你好"
    cache1.close()

    # Simulate a restart: open a fresh Cache instance over the same directory.
    cache2 = diskcache.Cache(cache_dir)
    result2 = translator._cached_translate("hello", "en", "zh-TW", cache=cache2)
    assert result2 == "你好"
    cache2.close()

    # The underlying translator should only have been called once.
    assert mock_translate.call_count == 1


@pytest.mark.parametrize("failed_result", [None, ""])
def test_failed_translation_not_cached(tmp_path, monkeypatch, failed_result):
    cache_dir = str(tmp_path / "cache")
    mock_translate = Mock(return_value=failed_result)
    monkeypatch.setattr(translator, "_translate_with_fallback", mock_translate)

    cache = diskcache.Cache(cache_dir)
    result1 = translator._cached_translate("hello", "en", "zh-TW", cache=cache)
    result2 = translator._cached_translate("hello", "en", "zh-TW", cache=cache)
    cache.close()

    assert result1 == failed_result
    assert result2 == failed_result
    # Both calls should hit the translator since failures aren't cached.
    assert mock_translate.call_count == 2
