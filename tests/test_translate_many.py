import pytest

import translator


@pytest.mark.parametrize(
    ("text", "expected_source"),
    [
        ("money不是monkey", "zh-TW"),
        ("純中文訊息", "auto"),
        ("東京", "auto"),
        ("moneyではmonkey", "auto"),
        ("money는monkey", "auto"),
        ("money is not monkey", "auto"),
    ],
)
def test_auto_source_hints_chinese_without_overriding_japanese_or_korean(
    monkeypatch, text, expected_source
):
    calls = []

    class Chain:
        def translate_many(self, provider_text, source, targets):
            calls.append((provider_text, source, list(targets)))
            return {target: "translated" for target in targets}

    monkeypatch.setattr(translator, "_get_provider_chain", lambda: Chain())

    translator.translate_many_with_status(text, ["en"], _use_cache=False)

    assert calls == [(text, expected_source, ["en"])]


def test_inferred_chinese_source_preserves_same_language_destination(monkeypatch):
    calls = []

    class Chain:
        def translate_many(self, text, source, targets):
            calls.append((text, source, list(targets)))
            return {"en": "money is not monkey"}

    monkeypatch.setattr(translator, "_get_provider_chain", lambda: Chain())

    result = translator.translate_many_with_status(
        "money不是monkey", ["zh-TW", "en"], _use_cache=False
    )

    assert calls == [("money不是monkey", "zh-TW", ["en"])]
    assert result == {
        "zh-TW": translator.TranslationOutcome("money不是monkey", True),
        "en": translator.TranslationOutcome("money is not monkey", True),
    }


def test_translate_many_batches_unique_targets(monkeypatch):
    calls = []

    class Chain:
        def translate_many(self, text, source, targets):
            calls.append((text, source, list(targets)))
            return {"en": "Hello", "ja": "こんにちは", "de": "Hallo"}

    monkeypatch.setattr(translator, "_get_provider_chain", lambda: Chain())
    result = translator.translate_many_with_status(
        "你好", ["ja", "en", "de", "ja"], _use_cache=False
    )
    assert calls == [("你好", "auto", ["ja", "en", "de"])]
    assert {k: v.text for k, v in result.items()} == {
        "ja": "こんにちは", "en": "Hello", "de": "Hallo"
    }
    assert all(outcome.provider_succeeded for outcome in result.values())


@pytest.mark.parametrize(
    "glossary, expected_calls, expected_texts",
    [
        ({"Jim": {"en": "James", "ja": "ジム"}},
         [("Hello §0§", "auto", ["en", "ja"])],
         {"en": "Hello James", "ja": "Hello ジム"}),
        ({"Jim": {"en": "James"}},
         [("Hello §0§", "auto", ["en"]), ("Hello Jim", "auto", ["ja"])],
         {"en": "Hello James", "ja": "Hello Jim"}),
    ],
)
def test_target_glossary_controls_grouping_and_restoration(
    monkeypatch, glossary, expected_calls, expected_texts
):
    calls = []

    class Chain:
        def translate_many(self, text, source, targets):
            calls.append((text, source, list(targets)))
            return {target: text for target in targets}

    monkeypatch.setattr(translator, "_get_provider_chain", lambda: Chain())
    result = translator.translate_many_with_status(
        "Hello Jim", ["en", "ja"], glossary=glossary, _use_cache=False
    )
    assert calls == expected_calls
    assert {target: outcome.text for target, outcome in result.items()} == expected_texts
    assert all(outcome.provider_succeeded for outcome in result.values())


def test_multiline_groups_shared_and_target_specific_segments(monkeypatch):
    calls = []

    class Chain:
        def translate_many(self, text, source, targets):
            calls.append((text, source, list(targets)))
            return {target: f"[{text}]" for target in targets}

    monkeypatch.setattr(translator, "_get_provider_chain", lambda: Chain())
    result = translator.translate_many_with_status(
        "Hi Jim\n\nbye 👀\nbye 👀", ["en", "ja"],
        glossary={"Jim": {"en": "James"}}, _use_cache=False,
    )
    assert calls == [
        ("Hi §0§", "auto", ["en"]),
        ("bye", "auto", ["en", "ja"]),
        ("Hi Jim", "auto", ["ja"]),
    ]
    assert result["en"].text == "[Hi James]\n\n[bye]  👀\n[bye]  👀"
    assert result["ja"].text == "[Hi Jim]\n\n[bye]  👀\n[bye]  👀"


def test_partial_failure_preserves_only_failed_target_and_status(monkeypatch):
    class Chain:
        def translate_many(self, text, source, targets):
            return {"en": "hello", "ja": None}

    monkeypatch.setattr(translator, "_get_provider_chain", lambda: Chain())
    result = translator.translate_many_with_status("你好", ["en", "ja"], _use_cache=False)
    assert result == {
        "en": translator.TranslationOutcome("hello", True),
        "ja": translator.TranslationOutcome("你好", False),
    }


def test_failed_segment_marks_target_failed_and_keeps_literal_fallback(monkeypatch):
    class Chain:
        def translate_many(self, text, source, targets):
            return {target: None if text == "Hello §0§" else "Bye" for target in targets}

    monkeypatch.setattr(translator, "_get_provider_chain", lambda: Chain())
    result = translator.translate_many_with_status(
        "Hello Jim 👀 https://example.com\n再見", ["en", "ja"],
        glossary={"Jim": {"en": "James", "ja": "ジム"}}, _use_cache=False,
    )
    assert result["en"] == translator.TranslationOutcome(
        "Hello James  https://example.com  👀\nBye", False
    )
    assert result["ja"] == translator.TranslationOutcome(
        "Hello ジム  https://example.com  👀\nBye", False
    )


def test_full_glossary_and_mentions_need_no_provider(monkeypatch):
    def unexpected_provider():
        pytest.fail("a glossary-only message must not contact a provider")

    monkeypatch.setattr(translator, "_get_provider_chain", unexpected_provider)
    result = translator.translate_many_with_status(
        "<@123484> Jim 👀 <:wave:123> https://example.com", ["en", "ja"],
        glossary={"Jim": {"en": "James", "ja": "ジム"}},
        substitutions={"484": "broken"}, _use_cache=False,
    )
    assert result["en"] == translator.TranslationOutcome(
        "James  https://example.com  👀  <:wave:123>  <@123484>", True
    )
    assert result["ja"] == translator.TranslationOutcome(
        "ジム  https://example.com  👀  <:wave:123>  <@123484>", True
    )


def test_batch_canonicalizes_targets_without_an_allowlist(monkeypatch):
    calls = []

    class Chain:
        def translate_many(self, text, source, targets):
            calls.append((source, targets))
            return {target: target for target in targets}

    monkeypatch.setattr(translator, "_get_provider_chain", lambda: Chain())
    result = translator.translate_many_with_status(
        "hello", [" EN ", "en", "zh-tw", "zh-TW", "de"], _use_cache=False
    )
    assert calls == [("auto", ["en", "zh-TW", "de"])]
    assert list(result) == ["en", "zh-TW", "de"]


@pytest.mark.parametrize("text, expected", [("", None), ("<@123>", "<@123>"), ("👀", "👀")])
def test_batch_passthrough_is_success_without_provider(monkeypatch, text, expected):
    def unexpected_provider():
        pytest.fail("passthrough must not contact a provider")

    monkeypatch.setattr(translator, "_get_provider_chain", unexpected_provider)
    result = translator.translate_many_with_status(text, ["en", "ja"], _use_cache=False)
    assert result == {target: translator.TranslationOutcome(expected, True) for target in ["en", "ja"]}


def test_status_facade_can_skip_probe_and_bounds_probe_timeout(monkeypatch):
    probes = []

    class Chain:
        def status_snapshot(self):
            return {"azure": {"configured": True}}

        def probe_libretranslate(self, *, timeout):
            probes.append(timeout)
            return {"success": True}

    monkeypatch.setattr(translator, "_get_provider_chain", lambda: Chain())
    assert translator.get_translation_status(probe_libre=False) == {
        "azure": {"configured": True}, "libretranslate_probe": None
    }
    assert probes == []
    assert translator.get_translation_status() == {
        "azure": {"configured": True}, "libretranslate_probe": {"success": True}
    }
    assert probes == [5.0]


@pytest.mark.parametrize("text", ["Jim 👀 https://example.com", "Hello Jim 👀 https://example.com"])
def test_empty_glossary_render_preserves_original_line(monkeypatch, text):
    class Chain:
        def translate_many(self, text, source, targets):
            return {target: "§0§" for target in targets}

    monkeypatch.setattr(translator, "_get_provider_chain", lambda: Chain())
    result = translator.translate_many_with_status(
        text, ["en", "ja"], glossary={"Jim": {"*": ""}}, _use_cache=False
    )
    assert result == {target: translator.TranslationOutcome(text, True) for target in ["en", "ja"]}
