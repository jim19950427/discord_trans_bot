from unittest.mock import Mock

import diskcache

import translator


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


def test_failed_translation_not_cached(tmp_path, monkeypatch):
    cache_dir = str(tmp_path / "cache")
    mock_translate = Mock(return_value=None)
    monkeypatch.setattr(translator, "_translate_with_fallback", mock_translate)

    cache = diskcache.Cache(cache_dir)
    result1 = translator._cached_translate("hello", "en", "zh-TW", cache=cache)
    result2 = translator._cached_translate("hello", "en", "zh-TW", cache=cache)
    cache.close()

    assert result1 is None
    assert result2 is None
    # Both calls should hit the translator since failures aren't cached.
    assert mock_translate.call_count == 2
