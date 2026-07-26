import translator


class _EchoTranslator:
    """Fake GoogleTranslator that always returns the input unchanged."""

    def __init__(self, source, target):
        self.source = source
        self.target = target

    def translate(self, text):
        return text


class _RaisingTranslator:
    """Fake GoogleTranslator that always raises a given exception."""

    def __init__(self, source, target):
        self.source = source
        self.target = target

    def translate(self, text):
        raise Exception("429 Too Many Requests")


def test_same_input_result_uses_short_retry_budget(monkeypatch):
    """Regression test: a result equal to the input (timestamps, leftover
    glossary placeholders, decoratively-spaced text) isn't a transient
    failure — retrying it with the full exponential backoff (up to 2+4+8=14s)
    just wastes time. It should give up after a couple of quick retries.
    """
    monkeypatch.setattr(translator, "GoogleTranslator", _EchoTranslator)
    sleep_calls = []
    monkeypatch.setattr(translator.time, "sleep", lambda s: sleep_calls.append(s))

    result = translator._try_google("hello", "en", "fr")

    assert result is None
    assert sleep_calls == [1]


def test_retryable_exception_still_uses_full_exponential_backoff(monkeypatch):
    """The genuinely transient case (rate limits) must keep the existing
    4-attempt exponential backoff — only the same-input case should be
    shortened.
    """
    monkeypatch.setattr(translator, "GoogleTranslator", _RaisingTranslator)
    sleep_calls = []
    monkeypatch.setattr(translator.time, "sleep", lambda s: sleep_calls.append(s))

    result = translator._try_google("hello", "en", "fr", retries=4)

    assert result is None
    assert sleep_calls == [1, 2, 4]


def test_eventual_success_after_same_input_retry(monkeypatch):
    calls = {"n": 0}

    class _EventuallyDifferentTranslator:
        def __init__(self, source, target):
            pass

        def translate(self, text):
            calls["n"] += 1
            return text if calls["n"] == 1 else "bonjour"

    monkeypatch.setattr(translator, "GoogleTranslator", _EventuallyDifferentTranslator)
    monkeypatch.setattr(translator.time, "sleep", lambda s: None)

    result = translator._try_google("hello", "en", "fr")

    assert result == "bonjour"
