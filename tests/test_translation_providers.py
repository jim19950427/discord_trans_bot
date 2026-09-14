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
    assert kwargs["params"] == {"api-version": "3.0", "from": "en", "to": "zh-Hant"}
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
