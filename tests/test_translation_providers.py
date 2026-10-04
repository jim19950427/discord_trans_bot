import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from translation_providers import (
    CircuitBreaker,
    ProviderError,
    ProviderSettings,
    TranslationProviderChain,
    map_language,
)


def test_settings_defaults(monkeypatch):
    for name in (
        "TRANSLATION_PROVIDER_ORDER",
        "AZURE_TRANSLATOR_KEY",
        "AZURE_TRANSLATOR_REGION",
        "AZURE_TRANSLATOR_ENDPOINT",
        "AZURE_TRANSLATOR_TIMEOUT_SECONDS",
        "LIBRETRANSLATE_URL",
        "LIBRETRANSLATE_TIMEOUT_SECONDS",
        "AZURE_CIRCUIT_FAILURE_THRESHOLD",
        "AZURE_CIRCUIT_COOLDOWN_SECONDS",
        "LIBRETRANSLATE_MAX_CONCURRENCY",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = ProviderSettings.from_env()

    assert settings.provider_order == ("azure", "libretranslate")
    assert settings.azure_key is None
    assert settings.azure_region == "eastasia"
    assert settings.azure_endpoint == "https://api.cognitive.microsofttranslator.com"
    assert settings.azure_timeout == 5.0
    assert settings.libretranslate_url == "http://libretranslate:5000"
    assert settings.libretranslate_timeout == 30.0
    assert settings.circuit_failure_threshold == 3
    assert settings.circuit_cooldown == 300.0
    assert settings.libretranslate_max_concurrency == 2


def test_settings_filter_unknown_provider_and_strip_urls(monkeypatch):
    monkeypatch.setenv("TRANSLATION_PROVIDER_ORDER", " unknown, libretranslate, azure ")
    monkeypatch.setenv("AZURE_TRANSLATOR_ENDPOINT", "https://azure.example/")
    monkeypatch.setenv("LIBRETRANSLATE_URL", "http://libretranslate:5000/")

    with pytest.warns(RuntimeWarning, match="unknown"):
        settings = ProviderSettings.from_env()

    assert settings.provider_order == ("libretranslate", "azure")
    assert settings.azure_endpoint == "https://azure.example"
    assert settings.libretranslate_url == "http://libretranslate:5000"


@pytest.mark.parametrize(
    ("code", "provider", "expected"),
    [
        ("zh-TW", "azure", "zh-Hant"),
        ("zh-tw", "azure", "zh-Hant"),
        ("zh-TW", "libretranslate", "zt"),
        ("EN", "azure", "en"),
        ("ja", "libretranslate", "ja"),
        ("auto", "azure", "auto"),
    ],
)
def test_map_language(code, provider, expected):
    assert map_language(code, provider) == expected


@pytest.mark.parametrize("code", ["en", "es", "fr", "ja", "ko", "pl", "ru", "th"])
@pytest.mark.parametrize("provider", ["azure", "libretranslate"])
def test_non_chinese_configured_codes_are_shared_by_both_providers(code, provider):
    assert map_language(code, provider) == code


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_circuit_opens_after_threshold_and_recovers_with_probe():
    clock = FakeClock()
    circuit = CircuitBreaker(3, 300, monotonic=clock)

    assert circuit.allow_request() == (True, "closed")
    assert circuit.record_failure() is None
    assert circuit.record_failure() is None
    assert circuit.record_failure() == "opened"
    assert circuit.state == "open"
    assert circuit.allow_request() == (False, "open")

    clock.now += 300
    assert circuit.allow_request() == (True, "half_open")
    assert circuit.allow_request() == (False, "half_open")
    assert circuit.record_success() == "closed"
    assert circuit.state == "closed"


def test_failed_half_open_probe_reopens_for_full_cooldown():
    clock = FakeClock()
    circuit = CircuitBreaker(1, 300, monotonic=clock)
    circuit.record_failure()
    clock.now += 300
    assert circuit.allow_request() == (True, "half_open")

    assert circuit.record_failure() == "opened"
    clock.now += 299
    assert circuit.allow_request() == (False, "open")


def test_only_one_thread_gets_half_open_probe():
    clock = FakeClock()
    circuit = CircuitBreaker(1, 300, monotonic=clock)
    circuit.record_failure()
    clock.now += 300
    gate = threading.Barrier(8)

    def ask():
        gate.wait()
        return circuit.allow_request()

    with ThreadPoolExecutor(max_workers=8) as pool:
        decisions = list(pool.map(lambda _: ask(), range(8)))

    assert decisions.count((True, "half_open")) == 1
    assert decisions.count((False, "half_open")) == 7


class FakeResponse:
    def __init__(self, status_code=200, payload=None, json_error=None):
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise self._json_error
        return self._payload


def make_settings(**overrides):
    values = dict(
        provider_order=("azure", "libretranslate"),
        azure_key="test-key",
        azure_region="eastasia",
        azure_endpoint="https://api.cognitive.microsofttranslator.com",
        azure_timeout=5.0,
        libretranslate_url="http://libretranslate:5000",
        libretranslate_timeout=30.0,
        circuit_failure_threshold=3,
        circuit_cooldown=300.0,
        libretranslate_max_concurrency=2,
    )
    values.update(overrides)
    return ProviderSettings(**values)


def test_azure_request_maps_traditional_chinese_and_keeps_equal_output():
    post = Mock(return_value=FakeResponse(
        payload=[{"translations": [{"text": "Jim", "to": "zh-Hant"}]}]
    ))
    chain = TranslationProviderChain(
        make_settings(provider_order=("azure",)), post=post
    )

    assert chain.translate("Jim", "en", "zh-TW") == "Jim"
    _, kwargs = post.call_args
    assert kwargs["params"] == [
        ("api-version", "3.0"), ("to", "zh-Hant"), ("from", "en")
    ]
    assert kwargs["json"] == [{"Text": "Jim"}]
    assert kwargs["timeout"] == 5.0
    assert kwargs["headers"]["Ocp-Apim-Subscription-Key"] == "test-key"
    assert kwargs["headers"]["Ocp-Apim-Subscription-Region"] == "eastasia"
    assert "X-ClientTraceId" in kwargs["headers"]


def test_azure_auto_detection_omits_from_parameter():
    post = Mock(return_value=FakeResponse(
        payload=[{"translations": [{"text": "hello", "to": "en"}]}]
    ))
    chain = TranslationProviderChain(
        make_settings(provider_order=("azure",)), post=post
    )

    assert chain.translate("你好", "auto", "en") == "hello"
    assert "from" not in post.call_args.kwargs["params"]


def test_azure_batch_uses_repeated_targets_and_response_language_keys():
    post = Mock(return_value=FakeResponse(payload=[{"translations": [
        {"text": "\u3053\u3093\u306b\u3061\u306f", "to": "ja"},
        {"text": "\u4f60\u597d", "to": "zh-Hant"},
        {"text": "Hallo", "to": "de"},
    ]}]))
    chain = TranslationProviderChain(
        make_settings(provider_order=("azure",)), post=post
    )

    result = chain.translate_many("hello", "auto", ["zh-TW", "de", "ja", "de"])

    assert result == {"zh-TW": "\u4f60\u597d", "de": "Hallo", "ja": "\u3053\u3093\u306b\u3061\u306f"}
    assert post.call_count == 1
    assert post.call_args.kwargs["params"] == [
        ("api-version", "3.0"), ("to", "zh-Hant"),
        ("to", "de"), ("to", "ja"),
    ]


def test_azure_partial_batch_falls_back_only_for_missing_target():
    post = Mock(side_effect=[
        FakeResponse(payload=[{"translations": [
            {"text": "bonjour", "to": "fr"},
        ]}]),
        FakeResponse(payload={"translatedText": "\u3053\u3093\u306b\u3061\u306f"}),
    ])
    chain = TranslationProviderChain(
        make_settings(), post=post, wall_clock=lambda: 1234.0
    )

    result = chain.translate_many("hello", "en", ["fr", "ja"])

    assert result == {"fr": "bonjour", "ja": "\u3053\u3093\u306b\u3061\u306f"}
    assert post.call_count == 2
    assert post.call_args_list[1].kwargs["json"]["target"] == "ja"
    assert chain.status_snapshot()["fallback"] == {
        "last_at": 1234.0, "reason": "partial_response"
    }


@pytest.mark.parametrize("duplicate_text", ["conflicting result", "", None])
def test_duplicate_azure_target_remains_unresolved_for_libre(duplicate_text):
    """An ambiguous repeated destination must never win over the fallback result."""
    post = Mock(side_effect=[
        FakeResponse(payload=[{"translations": [
            {"text": "bonjour", "to": "fr"},
            {"text": "first result", "to": "ja"},
            {"text": duplicate_text, "to": "JA"},
        ]}]),
        FakeResponse(payload={"translatedText": "こんにちは"}),
    ])
    chain = TranslationProviderChain(make_settings(), post=post)

    assert chain.translate_many("hello", "auto", ["fr", "ja"]) == {
        "fr": "bonjour", "ja": "こんにちは"
    }
    assert post.call_count == 2
    assert post.call_args_list[1].kwargs["json"]["target"] == "ja"


def test_azure_wire_aliases_share_one_target_and_resolve_every_requested_key():
    """Colliding provider aliases must not duplicate parameters or lose a result key."""
    post = Mock(return_value=FakeResponse(payload=[{"translations": [
        {"text": "你好", "to": "zh-Hant"},
    ]}]))
    chain = TranslationProviderChain(make_settings(), post=post)

    assert chain.translate_many("hello", "auto", ["zh-TW", "zh-Hant"]) == {
        "zh-TW": "你好", "zh-Hant": "你好"
    }
    assert post.call_count == 1
    assert post.call_args.kwargs["params"] == [
        ("api-version", "3.0"), ("to", "zh-Hant")
    ]


@pytest.mark.parametrize("attempt", ["success", "failure", "missing_key", "circuit_open"])
def test_azure_batch_events_include_sanitized_target_count(attempt):
    """Every emitted Azure batch event must expose its requested target count."""
    logs = []
    post = Mock(return_value=(
        FakeResponse(payload=[{"translations": [
            {"text": "private translation", "to": "fr"},
            {"text": "private translation", "to": "ja"},
        ]}]) if attempt == "success" else FakeResponse(status_code=500)
    ))
    chain = TranslationProviderChain(
        make_settings(
            provider_order=("azure",),
            azure_key=None if attempt == "missing_key" else "test-key",
        ),
        post=post,
        logger=lambda message, **fields: logs.append((message, fields)),
    )
    if attempt == "circuit_open":
        for _ in range(3):
            chain._circuit.record_failure()

    chain.translate_many("private phrase", "auto", ["fr", "ja", "fr"])

    assert len(logs) == 1
    assert logs[0][1]["target_count"] == 2
    assert logs[0][1]["dest"] == "fr,ja"
    for private_value in ("private phrase", "private translation", "test-key"):
        assert private_value not in repr(logs)


def test_failed_eight_target_azure_batch_counts_as_one_circuit_failure():
    chain = TranslationProviderChain(
        make_settings(provider_order=("azure",)),
        post=Mock(return_value=FakeResponse(status_code=500)),
    )

    result = chain.translate_many(
        "hello", "en", ["en", "es", "fr", "ja", "ko", "pl", "ru", "th"]
    )

    assert result == {
        "en": None, "es": None, "fr": None, "ja": None,
        "ko": None, "pl": None, "ru": None, "th": None,
    }
    assert chain._circuit._failures == 1


def test_missing_azure_key_skips_one_batch_attempt_then_uses_libre_per_target():
    logs = []
    post = Mock(side_effect=[
        FakeResponse(payload={"translatedText": "bonjour"}),
        FakeResponse(payload={"translatedText": "\u3053\u3093\u306b\u3061\u306f"}),
    ])
    chain = TranslationProviderChain(
        make_settings(azure_key=None),
        post=post,
        logger=lambda message, **fields: logs.append((message, fields)),
    )

    assert chain.translate_many("hello", "en", ["fr", "ja"]) == {
        "fr": "bonjour", "ja": "\u3053\u3093\u306b\u3061\u306f"
    }
    assert post.call_count == 2
    assert len([
        fields for _, fields in logs
        if fields.get("fallback_reason") == "missing_key"
    ]) == 1


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(status_code=429, payload={}),
        FakeResponse(status_code=401, payload={}),
        FakeResponse(status_code=403, payload={}),
        FakeResponse(status_code=500, payload={}),
        FakeResponse(status_code=200, json_error=ValueError("invalid json")),
        FakeResponse(status_code=200, payload=[]),
        FakeResponse(status_code=200, payload=[{}]),
        FakeResponse(status_code=200, payload=[{"translations": []}]),
        FakeResponse(status_code=200, payload=[{"translations": [{"text": ""}]}]),
    ],
)
def test_invalid_azure_response_returns_none(response):
    chain = TranslationProviderChain(
        make_settings(provider_order=("azure",)), post=Mock(return_value=response)
    )
    assert chain.translate("hello", "en", "fr") is None


def test_azure_request_exception_returns_none_without_secret_in_log():
    logs = []
    chain = TranslationProviderChain(
        make_settings(provider_order=("azure",)),
        post=Mock(side_effect=TimeoutError("test-key must not appear")),
        logger=lambda message, **fields: logs.append((message, fields)),
    )

    assert chain.translate("hello", "en", "fr") is None
    rendered = repr(logs)
    assert "test-key" not in rendered
    assert logs[-1][1]["provider"] == "azure"
    assert logs[-1][1]["success"] is False


def test_success_log_contains_provider_latency_and_no_request_content():
    logs = []
    post = Mock(return_value=FakeResponse(
        payload=[{"translations": [{"text": "bonjour", "to": "fr"}]}]
    ))
    chain = TranslationProviderChain(
        make_settings(provider_order=("azure",)),
        post=post,
        logger=lambda message, **fields: logs.append((message, fields)),
    )

    assert chain.translate("private phrase", "en", "fr") == "bonjour"
    message, fields = logs[-1]
    assert fields["type"] == "translate_provider"
    assert fields["provider"] == "azure"
    assert fields["success"] is True
    assert fields["latency_ms"] >= 0
    assert "private phrase" not in repr((message, fields))
    assert "test-key" not in repr((message, fields))


def test_azure_failure_falls_back_to_libretranslate_with_zt_mapping():
    post = Mock(side_effect=[
        FakeResponse(status_code=429, payload={}),
        FakeResponse(payload={"translatedText": "你好"}),
    ])
    chain = TranslationProviderChain(make_settings(), post=post)

    assert chain.translate("hello", "en", "zh-TW") == "你好"
    _, libre_kwargs = post.call_args_list[1]
    assert libre_kwargs["json"] == {
        "q": "hello",
        "source": "en",
        "target": "zt",
        "format": "text",
    }
    assert libre_kwargs["timeout"] == 30.0


def test_both_providers_fail_returns_none():
    post = Mock(side_effect=[
        FakeResponse(status_code=500, payload={}),
        FakeResponse(status_code=503, payload={}),
    ])
    assert TranslationProviderChain(make_settings(), post=post).translate(
        "hello", "en", "fr"
    ) is None


def test_provider_order_can_prefer_libretranslate():
    post = Mock(return_value=FakeResponse(payload={"translatedText": "bonjour"}))
    settings = make_settings(provider_order=("libretranslate", "azure"))

    assert TranslationProviderChain(settings, post=post).translate(
        "hello", "en", "fr"
    ) == "bonjour"
    assert post.call_count == 1
    assert post.call_args.args[0] == "http://libretranslate:5000/translate"


@pytest.mark.parametrize(
    ("text", "detected_language", "expected"),
    [
        ("那吃 zako", "zt", None),
        ("Already English", "en", "Already English"),
    ],
)
def test_libre_equal_output_depends_on_detected_language(
    text, detected_language, expected
):
    post = Mock(return_value=FakeResponse(payload={
        "detectedLanguage": {"confidence": 90.0, "language": detected_language},
        "translatedText": text,
    }))
    settings = make_settings(provider_order=("libretranslate",))

    result = TranslationProviderChain(settings, post=post).translate(
        text, "auto", "en"
    )

    assert result == expected


def test_missing_azure_key_skips_to_libretranslate():
    logs = []
    post = Mock(return_value=FakeResponse(payload={"translatedText": "bonjour"}))
    settings = make_settings(azure_key=None)
    chain = TranslationProviderChain(
        settings,
        post=post,
        logger=lambda message, **fields: logs.append((message, fields)),
    )

    assert chain.translate("hello", "en", "fr") == "bonjour"
    assert chain.translate("again", "en", "fr") == "bonjour"
    assert post.call_count == 2
    missing = [fields for _, fields in logs if fields.get("fallback_reason") == "missing_key"]
    assert len(missing) == 1


def test_three_azure_failures_open_circuit_and_fourth_call_skips_azure():
    clock = FakeClock()
    logs = []
    post = Mock(side_effect=[
        FakeResponse(status_code=500), FakeResponse(payload={"translatedText": "fr1"}),
        FakeResponse(status_code=500), FakeResponse(payload={"translatedText": "fr2"}),
        FakeResponse(status_code=500), FakeResponse(payload={"translatedText": "fr3"}),
        FakeResponse(payload={"translatedText": "fr4"}),
    ])
    chain = TranslationProviderChain(
        make_settings(),
        post=post,
        monotonic=clock,
        logger=lambda message, **fields: logs.append((message, fields)),
    )

    assert [chain.translate(f"hello {i}", "en", "fr") for i in range(4)] == [
        "fr1", "fr2", "fr3", "fr4"
    ]
    azure_calls = [c for c in post.call_args_list if "microsofttranslator" in c.args[0]]
    assert len(azure_calls) == 3
    assert any(fields.get("circuit_state") == "opened" for _, fields in logs)
    assert any(fields.get("fallback_reason") == "circuit_open" for _, fields in logs)


def test_stale_azure_success_does_not_close_open_circuit():
    first_started = threading.Event()
    release_first = threading.Event()
    calls_lock = threading.Lock()
    calls = 0

    def post(url, **kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
            call_number = calls
        if call_number == 1:
            first_started.set()
            assert release_first.wait(timeout=2)
            return FakeResponse(payload=[{"translations": [{"text": "first", "to": "fr"}]}])
        return FakeResponse(status_code=500)

    chain = TranslationProviderChain(
        make_settings(provider_order=("azure",)), post=post
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(chain.translate, "first", "en", "fr")
        assert first_started.wait(timeout=2)
        assert [chain.translate(f"failure {i}", "en", "fr") for i in range(3)] == [
            None,
            None,
            None,
        ]
        release_first.set()
        assert first.result(timeout=2) == "first"

    assert chain.translate("during cooldown", "en", "fr") is None
    assert calls == 4


def test_libretranslate_concurrency_is_capped_at_two():
    active = 0
    maximum = 0
    lock = threading.Lock()
    release = threading.Event()

    def post(url, **kwargs):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        release.wait(timeout=2)
        with lock:
            active -= 1
        return FakeResponse(payload={"translatedText": "ok"})

    settings = make_settings(provider_order=("libretranslate",))
    chain = TranslationProviderChain(settings, post=post)
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(chain.translate, str(i), "en", "fr") for i in range(6)]
        deadline = time.monotonic() + 1
        while maximum < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        release.set()
        assert [future.result(timeout=2) for future in futures] == ["ok"] * 6

    assert maximum == 2


def test_status_snapshot_is_sanitized_and_batch_aware():
    post = Mock(return_value=FakeResponse(payload=[{"translations": [
        {"text": "bonjour", "to": "fr"}, {"text": "\u3053\u3093\u306b\u3061\u306f", "to": "ja"},
    ]}]))
    chain = TranslationProviderChain(
        make_settings(), post=post, wall_clock=lambda: 1234.0
    )

    chain.translate_many("private phrase", "auto", ["fr", "ja"])

    status = chain.status_snapshot()
    assert status["azure"]["last_success_at"] == 1234.0
    assert status["azure"]["last_target_count"] == 2
    assert status["azure"]["circuit_state"] == "closed"
    assert status["azure"]["configured"] is True
    assert "private phrase" not in repr(status)
    assert "test-key" not in repr(status)


def test_status_snapshot_records_bounded_fallback_failure_without_request_content():
    chain = TranslationProviderChain(
        make_settings(provider_order=("azure",)),
        post=Mock(return_value=FakeResponse(status_code=503)),
        wall_clock=lambda: 2345.0,
    )

    assert chain.translate("private phrase", "en", "fr") is None

    status = chain.status_snapshot()
    assert status["azure"]["last_attempt_at"] == 2345.0
    assert status["azure"]["last_failure_at"] == 2345.0
    assert status["azure"]["last_failure_reason"] == "http_503"
    assert status["fallback"] == {"last_at": 2345.0, "reason": "http_503"}
    assert "private phrase" not in repr(status)
    assert "test-key" not in repr(status)


def test_probe_libretranslate_reports_sorted_languages_and_latency():
    get = Mock(return_value=FakeResponse(payload=[
        {"code": "ko"}, {"code": "zt"}, {"code": "en"}, {"code": "ja"},
    ]))
    elapsed = iter([10.0, 10.125])
    chain = TranslationProviderChain(
        make_settings(), get=get, monotonic=lambda: next(elapsed)
    )

    probe = chain.probe_libretranslate()

    assert get.call_args.args[0] == "http://libretranslate:5000/languages"
    assert get.call_args.kwargs["timeout"] == 5.0
    assert probe == {
        "healthy": True,
        "languages": ["en", "ja", "ko", "zt"],
        "latency_ms": 125,
        "failure_reason": None,
    }


@pytest.mark.parametrize(
    ("response", "expected_reason"),
    [
        (FakeResponse(status_code=503), "http_503"),
        (FakeResponse(payload={}), "invalid_response"),
    ],
)
def test_probe_libretranslate_reports_unhealthy_http_and_invalid_payload(response, expected_reason):
    chain = TranslationProviderChain(make_settings(), get=Mock(return_value=response))

    probe = chain.probe_libretranslate()

    assert probe["healthy"] is False
    assert probe["languages"] == []
    assert probe["failure_reason"] == expected_reason
    assert probe["latency_ms"] >= 0


def test_probe_libretranslate_reports_timeout_without_mutating_passive_health():
    chain = TranslationProviderChain(
        make_settings(), get=Mock(side_effect=TimeoutError("private phrase"))
    )
    before = chain.status_snapshot()

    probe = chain.probe_libretranslate(timeout=1.5)

    assert probe["healthy"] is False
    assert probe["languages"] == []
    assert probe["failure_reason"] == "request_error"
    assert chain.status_snapshot() == before


def _libre_only(**overrides):
    return make_settings(
        provider_order=("libretranslate",),
        libretranslate_circuit_failure_threshold=2,
        libretranslate_circuit_cooldown=30.0,
        **overrides,
    )


def test_libretranslate_outage_opens_breaker_and_skips_remaining_targets():
    post = Mock(return_value=FakeResponse(status_code=503))
    logs = []
    chain = TranslationProviderChain(
        _libre_only(), post=post, logger=lambda m, **f: logs.append(f)
    )

    results = chain.translate_many("hello", "en", ["fr", "ja", "ko", "ru"])

    assert results == {"fr": None, "ja": None, "ko": None, "ru": None}
    assert post.call_count == 2  # breaker opened after 2 failures
    assert any(f.get("fallback_reason") == "circuit_open" for f in logs)
    assert chain.status_snapshot()["libretranslate"]["circuit_state"] == "open"


def test_libretranslate_breaker_recovers_after_cooldown():
    clock = FakeClock()
    responses = [FakeResponse(status_code=503), FakeResponse(status_code=503)]
    post = Mock(side_effect=lambda *a, **k: responses.pop(0) if responses else
                FakeResponse(payload={"translatedText": "ok"}))
    chain = TranslationProviderChain(_libre_only(), post=post, monotonic=clock)

    assert chain.translate("a", "en", "fr") is None
    assert chain.translate("b", "en", "fr") is None
    assert chain.translate("c", "en", "fr") is None  # open: no request made
    assert post.call_count == 2
    clock.now += 31
    assert chain.translate("d", "en", "fr") == "ok"
    assert chain.status_snapshot()["libretranslate"]["circuit_state"] == "closed"


def test_libretranslate_content_errors_do_not_trip_breaker():
    post = Mock(return_value=FakeResponse(status_code=400))
    chain = TranslationProviderChain(_libre_only(), post=post)

    for _ in range(5):
        chain.translate("hello", "en", "fr")

    assert post.call_count == 5
    assert chain.status_snapshot()["libretranslate"]["circuit_state"] == "closed"


def test_default_transport_reuses_one_session_per_thread(monkeypatch):
    sessions = []

    class FakeSession:
        def __init__(self):
            sessions.append(self)
            self.calls = 0

        def post(self, *args, **kwargs):
            self.calls += 1
            return FakeResponse(payload={"translatedText": "ok"})

    monkeypatch.setattr("translation_providers.requests.Session", FakeSession)
    chain = TranslationProviderChain(make_settings(provider_order=("libretranslate",)))

    chain.translate("a", "en", "fr")
    chain.translate("b", "en", "fr")
    assert len(sessions) == 1 and sessions[0].calls == 2

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(chain.translate, "c", "en", "fr").result()
    assert len(sessions) == 2  # a different thread gets its own session
