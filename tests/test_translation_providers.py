import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from translation_providers import CircuitBreaker, ProviderSettings, map_language


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
