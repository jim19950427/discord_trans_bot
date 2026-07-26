from unittest.mock import Mock

import translator


def test_multiline_simple_message_translated_as_one_block(monkeypatch):
    """Regression test for a real incident: a message manually broken across
    lines (a common casual typing style, e.g. '帥氣的Jim\\n不要\\n打破\\n我的\\n幻想')
    was translated line-by-line independently, losing all sentence context
    and producing word-salad output ('打破' alone -> 'break in' instead of
    'break'). The whole block must go through translation in a single call
    so Google can see cross-line context.
    """
    mock_translate = Mock(return_value="Handsome Jim\nDon't break my fantasy")
    monkeypatch.setattr(translator, "_translate_with_fallback", mock_translate)

    text = "帥氣的Jim\n不要\n打破\n我的\n幻想"
    result = translator.translate_text(text, "zh-TW", "en", _use_cache=False)

    mock_translate.assert_called_once_with(text, "zh-TW", "en")
    assert result == "Handsome Jim\nDon't break my fantasy"


def test_multiline_with_glossary_uses_per_line_path(monkeypatch):
    mock_translate = Mock(side_effect=lambda t, s, d: f"[{t}]")
    monkeypatch.setattr(translator, "_translate_with_fallback", mock_translate)

    text = "hello\nworld"
    glossary = {"hello": {"en": "HELLO"}}
    translator.translate_text(text, "zh-TW", "en", glossary=glossary, _use_cache=False)

    called_args = [c.args[0] for c in mock_translate.call_args_list]
    assert text not in called_args


def test_multiline_with_inline_mixed_emoji_uses_per_line_path(monkeypatch):
    """A line that mixes real words with an emoji still needs per-line
    extraction — only whole emoji-only/URL-only lines are exempt."""
    mock_translate = Mock(side_effect=lambda t, s, d: f"[{t}]")
    monkeypatch.setattr(translator, "_translate_with_fallback", mock_translate)

    text = "hello 👀\nworld"
    translator.translate_text(text, "zh-TW", "en", _use_cache=False)

    called_args = [c.args[0] for c in mock_translate.call_args_list]
    assert text not in called_args


def test_multiline_with_inline_mixed_url_uses_per_line_path(monkeypatch):
    mock_translate = Mock(side_effect=lambda t, s, d: f"[{t}]")
    monkeypatch.setattr(translator, "_translate_with_fallback", mock_translate)

    text = "check this out https://example.com\nmore stuff here"
    translator.translate_text(text, "zh-TW", "en", _use_cache=False)

    called_args = [c.args[0] for c in mock_translate.call_args_list]
    assert text not in called_args


def test_multiline_trailing_emoji_only_line_uses_fast_path(monkeypatch):
    """A whole line that's just emoji (very common — a trailing reaction)
    doesn't need extraction, so it shouldn't block the fast path."""
    mock_translate = Mock(return_value="translated block")
    monkeypatch.setattr(translator, "_translate_with_fallback", mock_translate)

    text = "帥氣的Jim\n不要\n打破\n我的\n幻想\n🤯🤯🤯🤯"
    translator.translate_text(text, "zh-TW", "en", _use_cache=False)

    mock_translate.assert_called_once_with(text, "zh-TW", "en")


def test_multiline_standalone_url_line_uses_fast_path(monkeypatch):
    """A line that's just a URL (no other words) doesn't need extraction
    either — only a URL mixed inline with text does."""
    mock_translate = Mock(return_value="translated block")
    monkeypatch.setattr(translator, "_translate_with_fallback", mock_translate)

    text = "check this out\nhttps://example.com"
    translator.translate_text(text, "zh-TW", "en", _use_cache=False)

    mock_translate.assert_called_once_with(text, "zh-TW", "en")


def test_single_line_message_unaffected(monkeypatch):
    mock_translate = Mock(return_value="hi there")
    monkeypatch.setattr(translator, "_translate_with_fallback", mock_translate)

    result = translator.translate_text("你好", "zh-TW", "en", _use_cache=False)

    assert result == "hi there"
    mock_translate.assert_called_once()


def test_unrelated_glossary_does_not_block_fast_path(monkeypatch):
    """Regression test for a real incident: a guild-wide glossary of proper
    nouns (e.g. "Jim", "Maria") disabled the fast path for every message on
    that guild, even when the message never used any of those terms —
    because the old check was "does this guild have any glossary" instead
    of "does this line actually match one".
    """
    mock_translate = Mock(return_value="translated block")
    monkeypatch.setattr(translator, "_translate_with_fallback", mock_translate)

    glossary = {"Jim": {"*": "Jim"}, "Maria": {"*": "Maria"}}
    text = "帥氣的\n不要\n打破\n我的\n幻想"
    translator.translate_text(text, "zh-TW", "en", glossary=glossary, _use_cache=False)

    mock_translate.assert_called_once_with(text, "zh-TW", "en")


def test_hidden_control_characters_do_not_fragment_message(monkeypatch):
    """Regression test for a real production incident: a message copy-pasted
    from another app contained hidden \\x1d (Group Separator) control
    characters between nearly every word. Discord never renders them, but
    str.splitlines() treats them as line breaks, fragmenting the message
    into per-word translate calls and destroying all context (e.g. "如果"
    alone -> "if", "错" alone -> "wrong", instead of one coherent sentence).
    """
    mock_translate = Mock(return_value="translated whole block")
    monkeypatch.setattr(translator, "_translate_with_fallback", mock_translate)

    text = (
        "如果\x1d\x1d是\x1d\x1d我\x1d\x1d的\x1d\x1d错\x1d\x1d的话\x1d\x1d非常\x1d\x1d非常"
        "\x1d\x1d抱歉\x1d\x1d知道\x1d\x1dbut I know it's not my fault \n"
        "谢谢\x1d\x1d担心\x1d\x1d我\x1d mmmmmmuwahhhhh"
    )
    translator.translate_text(text, "zh-TW", "en", _use_cache=False)

    mock_translate.assert_called_once()
    called_text = mock_translate.call_args[0][0]
    assert "\x1d" not in called_text
