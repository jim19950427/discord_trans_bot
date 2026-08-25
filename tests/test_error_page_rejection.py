"""Regression tests for Google serving its error page as a translation.

In Aug 2026 Google began blocking deep-translator's default
"python-requests/x.y" User-Agent: translate.google.com/m answered HTTP 200
(so deep-translator's status check passed) but put an "Error 500 (Server
Error)" page in the result slot. Because that text doesn't equal the input,
_try_google accepted it as a successful translation, the bot posted it into
Discord, and _cached_translate wrote it to the persistent cache forever.
"""

import translator

ERROR_PAGE = (
    "Error 500 (Server Error)!!1500.That’s an error."
    "There was an error. Please try again later.That’s all we know."
)


class _ErrorPageTranslator:
    """Fake GoogleTranslator that returns Google's error page as a result."""

    def __init__(self, source, target):
        pass

    def translate(self, text):
        return ERROR_PAGE


def test_error_page_is_not_returned_as_translation(monkeypatch):
    monkeypatch.setattr(translator, "GoogleTranslator", _ErrorPageTranslator)
    monkeypatch.setattr(translator.time, "sleep", lambda s: None)

    assert translator._try_google("屁眼？", "zh-TW", "en") is None


def test_error_page_uses_short_retry_budget(monkeypatch):
    """A failing Google is persistent, not transient — don't burn the full
    2/4/8s backoff on every language of every message."""
    monkeypatch.setattr(translator, "GoogleTranslator", _ErrorPageTranslator)
    sleep_calls = []
    monkeypatch.setattr(translator.time, "sleep", lambda s: sleep_calls.append(s))

    translator._try_google("hello", "en", "fr")

    assert sleep_calls == [1]


def test_error_page_is_never_cached(monkeypatch):
    """The real damage was persistence: a poisoned entry is served forever
    with no API call and no retry."""
    monkeypatch.setattr(translator, "GoogleTranslator", _ErrorPageTranslator)
    monkeypatch.setattr(translator.time, "sleep", lambda s: None)
    cache = {}

    result = translator._cached_translate("야", "ko", "fr", cache=cache)

    assert result is None
    assert cache == {}


def test_genuine_translation_still_accepted(monkeypatch):
    class _GoodTranslator:
        def __init__(self, source, target):
            pass

        def translate(self, text):
            return "Butthole?"

    monkeypatch.setattr(translator, "GoogleTranslator", _GoodTranslator)
    assert translator._try_google("屁眼？", "zh-TW", "en") == "Butthole?"


def test_does_not_misfire_when_source_text_contains_the_phrase():
    """A user writing "that's all we know" must not have their translation
    thrown away."""
    src = "well that’s all we know so far"
    assert translator._looks_like_error_page("c’est tout ce que nous savons", src) is False
    assert translator._looks_like_error_page("that’s all we know", src) is False


def test_detects_both_error_page_signatures():
    assert translator._looks_like_error_page(ERROR_PAGE, "屁眼？") is True
    assert translator._looks_like_error_page("Error 502 (Server Error)!!1", "hi") is True
    assert translator._looks_like_error_page("a normal translation", "hi") is False


def test_browser_user_agent_is_sent(monkeypatch):
    """The actual fix: without a browser UA Google returns the error page."""
    import deep_translator.google as dg

    captured = {}

    class _FakeReal:
        def get(self, *args, **kwargs):
            captured.update(kwargs.get("headers") or {})
            raise RuntimeError("stop here")

    proxy = translator._UARequestsProxy(_FakeReal())
    try:
        proxy.get("https://example.com")
    except RuntimeError:
        pass

    assert "Mozilla/5.0" in captured.get("User-Agent", "")
    assert "python-requests" not in captured.get("User-Agent", "")
    # and the live module is actually patched
    assert isinstance(dg.requests, translator._UARequestsProxy)
