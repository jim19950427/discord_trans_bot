from unittest.mock import Mock

import translator


def test_substitution_does_not_corrupt_user_mention_id(monkeypatch):
    """A substitution term that happens to be a substring of a mention's
    numeric ID must not corrupt the mention. Reproduces a real incident where
    a "484" -> "是不是" substitution rule mangled <@...484...> into
    <@...是不是...> because the pre-translation substitution regex has no
    word-boundary protection and runs on the raw message text.
    """
    monkeypatch.setattr(translator, "_translate_with_fallback", Mock(return_value="translated"))
    text = "<@1234567890484567> hello"
    subs = {"484": "是不是"}

    result = translator.translate_text(text, "en", "zh-TW", substitutions=subs, _use_cache=False)

    assert "<@1234567890484567>" in result
    assert "是不是" not in result


def test_glossary_does_not_corrupt_user_mention_id(monkeypatch):
    monkeypatch.setattr(translator, "_translate_with_fallback", Mock(return_value="translated"))
    text = "<@1234567890484567> hello"
    glossary = {"484": {"zh-TW": "是不是"}}

    result = translator.translate_text(text, "en", "zh-TW", glossary=glossary, _use_cache=False)

    assert "<@1234567890484567>" in result
    assert "是不是" not in result


def test_mention_only_message_forwards_verbatim(monkeypatch):
    mock_translate = Mock(return_value="should not be called")
    monkeypatch.setattr(translator, "_translate_with_fallback", mock_translate)

    result = translator.translate_text("<@1234567890484567>", "en", "zh-TW", _use_cache=False)

    assert result == "<@1234567890484567>"
    mock_translate.assert_not_called()


def test_role_and_channel_mentions_protected(monkeypatch):
    monkeypatch.setattr(translator, "_translate_with_fallback", Mock(return_value="translated"))
    text = "<@&1234567890484567> and <#1234567890484567> hello"
    subs = {"484": "是不是"}

    result = translator.translate_text(text, "en", "zh-TW", substitutions=subs, _use_cache=False)

    assert "<@&1234567890484567>" in result
    assert "<#1234567890484567>" in result
    assert "是不是" not in result
