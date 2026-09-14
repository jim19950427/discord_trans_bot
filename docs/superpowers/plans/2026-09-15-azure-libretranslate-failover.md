# Azure Primary with LibreTranslate Failover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the blocked Google scraper with Azure Translator as the primary translation provider and a private LibreTranslate/Argos NAS service as the automatic fallback.

**Architecture:** Keep `translator.py` responsible for text preparation, glossary restoration, caching, and its current public API. Add a focused `translation_providers.py` containing provider configuration, language mapping, REST clients, a thread-safe Azure circuit breaker, and LibreTranslate concurrency control; add LibreTranslate as an internal-only service in the existing Compose project.

**Tech Stack:** Python 3.11, `requests`, pytest, Docker Compose, Azure Translator Text REST API v3, LibreTranslate v1.9.6, Argos Translate models.

**Spec:** `docs/superpowers/specs/2026-09-15-azure-libretranslate-failover-design.md`

## Global Constraints

- Keep `bot.py`'s imports and calls to `translate_text()`, `translate_text_nocache()`, `normalize_lang()`, `has_translatable_content()`, and `log_event()` compatible.
- Preserve the current substitutions, glossary placeholders, multiline behavior, mention/emoji/URL protection, disk cache, delayed retry, and per-channel failure isolation.
- Runtime provider order defaults exactly to `azure,libretranslate` and never includes the Google scraper.
- Azure resource values are `AZURE_TRANSLATOR_REGION=eastasia` and `AZURE_TRANSLATOR_ENDPOINT=https://api.cognitive.microsofttranslator.com`.
- Never put a real Azure key in source, documentation, tests, Git, command arguments, logs, or chat; the real value exists only in the NAS `.env`.
- Pin LibreTranslate to `libretranslate/libretranslate:v1.9.6`; do not use `latest`.
- Do not expose LibreTranslate with a host `ports` mapping.
- Load only `en,es,fr,ja,ko,pl,ru,th,zt`, use two LibreTranslate threads, and persist `/home/libretranslate/.local` in a named volume.
- Azure timeout is 5 seconds; LibreTranslate timeout is 30 seconds.
- Open the Azure circuit after 3 consecutive failures for 300 seconds, with exactly one half-open probe.
- Allow no more than 2 concurrent LibreTranslate translations per bot process.
- Treat a non-empty provider result equal to the input as success.
- Automated tests must not call Azure, Google, or the NAS.

## File Structure

- Create `translation_providers.py`: provider settings, mappings, REST validation, circuit breaker, provider chain, sanitized structured events.
- Create `tests/test_translation_providers.py`: deterministic unit tests for settings, mappings, both REST providers, fallback, circuit behavior, logging, and concurrency.
- Modify `translator.py`: remove Google-specific code, lazily construct the provider chain, preserve the existing `_translate_with_fallback()` seam and final translation logging.
- Modify `tests/test_translate_log.py`: mock the provider-chain seam instead of Google internals.
- Delete `tests/test_try_google_retry.py`: its behavior is obsolete and replaced by provider/circuit tests.
- Delete `tests/test_error_page_rejection.py`: the Google HTML-error regression is obsolete once the scraper is removed.
- Modify `requirements.txt`: replace `deep-translator` with an explicit `requests` dependency.
- Modify `docker-compose.yml`: add the internal LibreTranslate service, model volume, provider-module mount, and health check.
- Modify `deploy.sh`: upload `translation_providers.py` during code and dependency deployments.
- Modify `.env.example`, `DEPLOY.md`, `README.md`, and `CLAUDE.md`: document configuration, operations, and new invariants without secrets.

---

### Task 1: Provider Settings, Language Mapping, and Circuit Breaker

**Files:**
- Create: `translation_providers.py`
- Create: `tests/test_translation_providers.py`

**Interfaces:**
- Consumes: environment variables listed in Global Constraints; an injected monotonic clock `Callable[[], float]`.
- Produces: `ProviderSettings.from_env() -> ProviderSettings`, `map_language(code: str, provider: str) -> str`, and `CircuitBreaker` methods `allow_request() -> tuple[bool, str]`, `record_success() -> str | None`, `record_failure() -> str | None`, and `state -> str`.

- [ ] **Step 1: Write failing settings and mapping tests**

Create `tests/test_translation_providers.py` with these tests and shared imports:

```python
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
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run:

```bash
pytest tests/test_translation_providers.py -v
```

Expected: collection fails with `ModuleNotFoundError: No module named 'translation_providers'`.

- [ ] **Step 3: Implement immutable settings and provider-boundary mappings**

Create `translation_providers.py` with these definitions:

```python
from __future__ import annotations

import os
import threading
import time
import warnings
from dataclasses import dataclass
from typing import Callable


SUPPORTED_PROVIDERS = ("azure", "libretranslate")
_CANONICAL_CODES = {
    "auto": "auto",
    "en": "en",
    "es": "es",
    "fr": "fr",
    "ja": "ja",
    "ko": "ko",
    "pl": "pl",
    "ru": "ru",
    "th": "th",
    "zh-tw": "zh-TW",
}
_PROVIDER_OVERRIDES = {
    "azure": {"zh-TW": "zh-Hant"},
    "libretranslate": {"zh-TW": "zt"},
}


def canonicalize_language(code: str) -> str:
    return _CANONICAL_CODES.get(code.strip().lower(), code.strip())


def map_language(code: str, provider: str) -> str:
    canonical = canonicalize_language(code)
    return _PROVIDER_OVERRIDES.get(provider, {}).get(canonical, canonical)


@dataclass(frozen=True)
class ProviderSettings:
    provider_order: tuple[str, ...]
    azure_key: str | None
    azure_region: str
    azure_endpoint: str
    azure_timeout: float
    libretranslate_url: str
    libretranslate_timeout: float
    circuit_failure_threshold: int
    circuit_cooldown: float
    libretranslate_max_concurrency: int

    @classmethod
    def from_env(cls) -> "ProviderSettings":
        requested = tuple(
            item.strip().lower()
            for item in os.getenv(
                "TRANSLATION_PROVIDER_ORDER", "azure,libretranslate"
            ).split(",")
            if item.strip()
        )
        unknown = tuple(item for item in requested if item not in SUPPORTED_PROVIDERS)
        if unknown:
            warnings.warn(
                f"Ignoring unknown translation providers: {', '.join(unknown)}",
                RuntimeWarning,
                stacklevel=2,
            )
        order = tuple(item for item in requested if item in SUPPORTED_PROVIDERS)
        key = os.getenv("AZURE_TRANSLATOR_KEY") or None
        return cls(
            provider_order=order,
            azure_key=key,
            azure_region=os.getenv("AZURE_TRANSLATOR_REGION", "eastasia"),
            azure_endpoint=os.getenv(
                "AZURE_TRANSLATOR_ENDPOINT",
                "https://api.cognitive.microsofttranslator.com",
            ).rstrip("/"),
            azure_timeout=float(os.getenv("AZURE_TRANSLATOR_TIMEOUT_SECONDS", "5")),
            libretranslate_url=os.getenv(
                "LIBRETRANSLATE_URL", "http://libretranslate:5000"
            ).rstrip("/"),
            libretranslate_timeout=float(
                os.getenv("LIBRETRANSLATE_TIMEOUT_SECONDS", "30")
            ),
            circuit_failure_threshold=int(
                os.getenv("AZURE_CIRCUIT_FAILURE_THRESHOLD", "3")
            ),
            circuit_cooldown=float(
                os.getenv("AZURE_CIRCUIT_COOLDOWN_SECONDS", "300")
            ),
            libretranslate_max_concurrency=int(
                os.getenv("LIBRETRANSLATE_MAX_CONCURRENCY", "2")
            ),
        )
```

- [ ] **Step 4: Run settings and mapping tests**

Run:

```bash
pytest tests/test_translation_providers.py -v
```

Expected: all tests added in Step 1 pass.

- [ ] **Step 5: Write failing circuit-breaker tests**

Append:

```python
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
```

- [ ] **Step 6: Run circuit tests to verify they fail**

Run:

```bash
pytest tests/test_translation_providers.py -k circuit -v
```

Expected: import or construction fails because `CircuitBreaker` is not defined.

- [ ] **Step 7: Implement the thread-safe circuit breaker**

Add this class to `translation_providers.py`:

```python
class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int,
        cooldown: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.failure_threshold = failure_threshold
        self.cooldown = cooldown
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._failures = 0
        self._open_until = 0.0
        self._probe_in_flight = False

    @property
    def state(self) -> str:
        with self._lock:
            if self._probe_in_flight:
                return "half_open"
            if self._open_until > self._monotonic():
                return "open"
            return "closed"

    def allow_request(self) -> tuple[bool, str]:
        with self._lock:
            now = self._monotonic()
            if self._open_until <= 0:
                return True, "closed"
            if now < self._open_until:
                return False, "open"
            if self._probe_in_flight:
                return False, "half_open"
            self._probe_in_flight = True
            return True, "half_open"

    def record_success(self) -> str | None:
        with self._lock:
            changed = self._failures > 0 or self._open_until > 0 or self._probe_in_flight
            self._failures = 0
            self._open_until = 0.0
            self._probe_in_flight = False
            return "closed" if changed else None

    def record_failure(self) -> str | None:
        with self._lock:
            self._failures += 1
            if self._probe_in_flight or self._failures >= self.failure_threshold:
                self._open_until = self._monotonic() + self.cooldown
                self._probe_in_flight = False
                return "opened"
            return None
```

- [ ] **Step 8: Run Task 1 tests and commit**

Run:

```bash
pytest tests/test_translation_providers.py -v
```

Expected: all Task 1 tests pass.

Commit:

```bash
git add translation_providers.py tests/test_translation_providers.py
git commit -m "feat: add translation provider settings and circuit breaker"
```

---

### Task 2: Azure and LibreTranslate Provider Chain

**Files:**
- Modify: `translation_providers.py`
- Modify: `tests/test_translation_providers.py`

**Interfaces:**
- Consumes: `ProviderSettings`, `map_language()`, `CircuitBreaker`; injected HTTP callable compatible with `requests.post`; logger compatible with `log_event(message: str, **fields) -> None`.
- Produces: `ProviderError(category: str, detail: str)`, `TranslationProviderChain(settings, post=requests.post, monotonic=time.monotonic, logger=None)`, and `TranslationProviderChain.translate(text: str, source: str, target: str) -> str | None`.

- [ ] **Step 1: Write failing Azure request and validation tests**

Append the helpers and tests below:

```python
from translation_providers import ProviderError, TranslationProviderChain


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
```

- [ ] **Step 2: Run Azure tests to verify they fail**

Run:

```bash
pytest tests/test_translation_providers.py -k azure -v
```

Expected: import fails because `ProviderError` and `TranslationProviderChain` do not exist.

- [ ] **Step 3: Implement provider errors, Azure REST validation, and attempt logging**

Add imports and definitions to `translation_providers.py`:

```python
import uuid
from typing import Any

import requests


class ProviderError(RuntimeError):
    def __init__(self, category: str, detail: str = ""):
        super().__init__(category)
        self.category = category
        self.detail = detail[:160]


def _response_text(payload: Any, provider: str) -> str:
    try:
        if provider == "azure":
            text = payload[0]["translations"][0]["text"]
        else:
            text = payload["translatedText"]
    except (IndexError, KeyError, TypeError) as exc:
        raise ProviderError("invalid_response", type(exc).__name__) from exc
    if not isinstance(text, str) or not text.strip():
        raise ProviderError("empty_response")
    return text
```

Implement `TranslationProviderChain.__init__()`, `_emit()`, `_azure_translate()`, and the Azure branch of `translate()`:

```python
class TranslationProviderChain:
    def __init__(
        self,
        settings: ProviderSettings,
        *,
        post: Callable[..., Any] = requests.post,
        monotonic: Callable[[], float] = time.monotonic,
        logger: Callable[..., None] | None = None,
    ):
        self.settings = settings
        self._post = post
        self._monotonic = monotonic
        self._logger = logger
        self._circuit = CircuitBreaker(
            settings.circuit_failure_threshold,
            settings.circuit_cooldown,
            monotonic=monotonic,
        )
        self._libre_slots = threading.BoundedSemaphore(
            settings.libretranslate_max_concurrency
        )
        self._diagnostic_lock = threading.Lock()
        self._missing_key_logged = False

    def _emit(self, provider: str, source: str, target: str, **fields) -> None:
        if self._logger:
            self._logger(
                f"[translate_provider] {provider} {source}->{target} "
                f"success={fields.get('success')}",
                type="translate_provider",
                provider=provider,
                src=source,
                dest=target,
                **fields,
            )

    def _azure_translate(self, text: str, source: str, target: str) -> str:
        params = {"api-version": "3.0", "to": map_language(target, "azure")}
        if source.lower() != "auto":
            params["from"] = map_language(source, "azure")
        headers = {
            "Ocp-Apim-Subscription-Key": self.settings.azure_key,
            "Ocp-Apim-Subscription-Region": self.settings.azure_region,
            "Content-Type": "application/json",
            "X-ClientTraceId": str(uuid.uuid4()),
        }
        try:
            response = self._post(
                f"{self.settings.azure_endpoint}/translate",
                params=params,
                headers=headers,
                json=[{"Text": text}],
                timeout=self.settings.azure_timeout,
            )
        except Exception as exc:
            raise ProviderError("request_error", type(exc).__name__) from exc
        if not 200 <= response.status_code < 300:
            raise ProviderError(f"http_{response.status_code}")
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise ProviderError("invalid_json", type(exc).__name__) from exc
        return _response_text(payload, "azure")

    def _log_missing_key_once(self, source: str, target: str) -> None:
        with self._diagnostic_lock:
            if self._missing_key_logged:
                return
            self._missing_key_logged = True
        self._emit(
            "azure",
            source,
            target,
            success=False,
            latency_ms=0,
            fallback_reason="missing_key",
            circuit_state=self._circuit.state,
        )

    def translate(self, text: str, source: str, target: str) -> str | None:
        for provider in self.settings.provider_order:
            if provider != "azure":
                continue
            if not self.settings.azure_key:
                self._log_missing_key_once(source, target)
                continue
            allowed, state = self._circuit.allow_request()
            if not allowed:
                self._emit(
                    "azure", source, target,
                    success=False,
                    latency_ms=0,
                    fallback_reason="circuit_open",
                    circuit_state=state,
                )
                continue
            started = self._monotonic()
            try:
                result = self._azure_translate(text, source, target)
            except ProviderError as error:
                transition = self._circuit.record_failure()
                self._emit(
                    "azure", source, target,
                    success=False,
                    latency_ms=int((self._monotonic() - started) * 1000),
                    fallback_reason=error.category,
                    error_detail=error.detail or None,
                    circuit_state=transition or self._circuit.state,
                )
                continue
            transition = self._circuit.record_success()
            self._emit(
                "azure", source, target,
                success=True,
                latency_ms=int((self._monotonic() - started) * 1000),
                fallback_reason=None,
                circuit_state=transition or self._circuit.state,
            )
            return result
        return None
```

The implementation emits only `ProviderError.category` plus its bounded `detail`. It never logs exception `repr`, request headers, or response bodies. `_log_missing_key_once()` makes the missing-key diagnostic process-wide for this chain instance rather than repeating it for every message.

Use these exact fields on each attempt:

```python
{
    "success": True | False,
    "latency_ms": int((self._monotonic() - started) * 1000),
    "fallback_reason": None | error.category | "missing_key" | "circuit_open",
    "circuit_state": self._circuit.state,
}
```

- [ ] **Step 4: Run Azure tests**

Run:

```bash
pytest tests/test_translation_providers.py -k azure -v
```

Expected: all Azure tests pass.

- [ ] **Step 5: Write failing LibreTranslate, fallback, order, and concurrency tests**

Append:

```python
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
```

- [ ] **Step 6: Run the new provider-chain tests to verify they fail**

Run:

```bash
pytest tests/test_translation_providers.py -k "libretranslate or fallback or provider_order or circuit" -v
```

Expected: LibreTranslate and fallback assertions fail because only Azure is implemented.

- [ ] **Step 7: Implement LibreTranslate and finish `translate()`**

Add:

```python
    def _libretranslate_translate(self, text: str, source: str, target: str) -> str:
        try:
            with self._libre_slots:
                response = self._post(
                    f"{self.settings.libretranslate_url}/translate",
                    json={
                        "q": text,
                        "source": map_language(source, "libretranslate"),
                        "target": map_language(target, "libretranslate"),
                        "format": "text",
                    },
                    timeout=self.settings.libretranslate_timeout,
                )
        except Exception as exc:
            raise ProviderError("request_error", type(exc).__name__) from exc
        if not 200 <= response.status_code < 300:
            raise ProviderError(f"http_{response.status_code}")
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise ProviderError("invalid_json", type(exc).__name__) from exc
        return _response_text(payload, "libretranslate")
```

Replace the Azure-only `translate()` from Step 3 with the complete loop:

```python
    def translate(self, text: str, source: str, target: str) -> str | None:
        for provider in self.settings.provider_order:
            if provider == "azure":
                if not self.settings.azure_key:
                    self._log_missing_key_once(source, target)
                    continue
                allowed, state = self._circuit.allow_request()
                if not allowed:
                    self._emit(
                        "azure", source, target,
                        success=False,
                        latency_ms=0,
                        fallback_reason="circuit_open",
                        circuit_state=state,
                    )
                    continue
                started = self._monotonic()
                try:
                    result = self._azure_translate(text, source, target)
                except ProviderError as error:
                    transition = self._circuit.record_failure()
                    self._emit(
                        "azure", source, target,
                        success=False,
                        latency_ms=int((self._monotonic() - started) * 1000),
                        fallback_reason=error.category,
                        error_detail=error.detail or None,
                        circuit_state=transition or self._circuit.state,
                    )
                    continue
                transition = self._circuit.record_success()
                self._emit(
                    "azure", source, target,
                    success=True,
                    latency_ms=int((self._monotonic() - started) * 1000),
                    fallback_reason=None,
                    circuit_state=transition or self._circuit.state,
                )
                return result

            if provider == "libretranslate":
                started = self._monotonic()
                try:
                    result = self._libretranslate_translate(text, source, target)
                except ProviderError as error:
                    self._emit(
                        "libretranslate", source, target,
                        success=False,
                        latency_ms=int((self._monotonic() - started) * 1000),
                        fallback_reason=error.category,
                        error_detail=error.detail or None,
                        circuit_state=self._circuit.state,
                    )
                    continue
                self._emit(
                    "libretranslate", source, target,
                    success=True,
                    latency_ms=int((self._monotonic() - started) * 1000),
                    fallback_reason=None,
                    circuit_state=self._circuit.state,
                )
                return result
        return None
```

This loop returns immediately on the first valid text, including unchanged text. For a half-open request, other threads receive `(False, "half_open")` and continue directly to LibreTranslate. The attempt causing a circuit transition logs `opened` or `closed`.

- [ ] **Step 8: Run all provider tests and commit**

Run:

```bash
pytest tests/test_translation_providers.py -v
```

Expected: all provider tests pass, including the two-call fallback and maximum concurrency of two.

Commit:

```bash
git add translation_providers.py tests/test_translation_providers.py
git commit -m "feat: add Azure and LibreTranslate provider chain"
```

---

### Task 3: Integrate the Provider Chain into the Existing Translation Pipeline

**Files:**
- Modify: `translator.py:1-69, 86, 95-119, 167-269, 351-409`
- Modify: `tests/test_translate_log.py`
- Delete: `tests/test_try_google_retry.py`
- Delete: `tests/test_error_page_rejection.py`

**Interfaces:**
- Consumes: `ProviderSettings.from_env()`, `TranslationProviderChain.translate(text, source, target) -> str | None`.
- Produces: unchanged public `translate_text()`, `translate_text_nocache()`, `normalize_lang()`, `has_translatable_content()`, and `log_event()`; retained private `_translate_with_fallback(text, src, dest) -> str | None`.

- [ ] **Step 1: Rewrite translate-log tests against the provider-chain seam**

Replace Google mocks in `tests/test_translate_log.py` with a chain mock:

```python
def _install_chain(monkeypatch, result):
    chain = Mock()
    chain.translate.side_effect = result if callable(result) else None
    if not callable(result):
        chain.translate.return_value = result
    monkeypatch.setattr(translator, "_get_provider_chain", lambda: chain)
    return chain
```

Update each test to call `_install_chain(...)` instead of monkeypatching `_try_google`. Keep existing assertions on final `type: "translate"`, `src`, `dest`, `input`, `output`, log capping, and corrupted-file recovery. Add:

```python
def test_provider_chain_receives_normalized_codes(tmp_path, monkeypatch):
    monkeypatch.setattr(translator, "LOG_FILE", str(tmp_path / "log.json"))
    chain = _install_chain(monkeypatch, "hello")

    assert translator._translate_with_fallback("你好", "zh-tw", "EN") == "hello"
    chain.translate.assert_called_once_with("你好", "zh-TW", "en")
```

- [ ] **Step 2: Run the modified log tests to verify they fail**

Run:

```bash
pytest tests/test_translate_log.py -v
```

Expected: tests fail because `_get_provider_chain` is not defined and Google code is still active.

- [ ] **Step 3: Remove Google-specific imports, constants, functions, and comments**

In `translator.py`:

- Remove imports of `deep_translator.google` and `GoogleTranslator`.
- Remove `_BROWSER_UA`, `_UARequestsProxy`, its monkeypatch, `_SUPPORTED` construction from Google, `_ERROR_PAGE_RE`, `_looks_like_error_page()`, `_try_google()`, and `_source_variants()`.
- Change Google-specific comments about custom emoji, URLs, multiline translation, and context to provider-neutral wording.
- Import `canonicalize_language`, `ProviderSettings`, and `TranslationProviderChain` from `translation_providers`.
- Implement `normalize_lang()` as `return canonicalize_language(code)`.

- [ ] **Step 4: Add the lazy provider-chain singleton and preserve the existing seam**

Add beside the lazy disk cache:

```python
_provider_chain: TranslationProviderChain | None = None
_provider_chain_lock = threading.Lock()


def _get_provider_chain() -> TranslationProviderChain:
    global _provider_chain
    if _provider_chain is None:
        with _provider_chain_lock:
            if _provider_chain is None:
                _provider_chain = TranslationProviderChain(
                    ProviderSettings.from_env(), logger=log_event
                )
    return _provider_chain
```

Define `_get_provider_chain()` after `log_event()` so the logger name exists when the singleton is constructed. Replace `_translate_with_fallback()` with:

```python
def _translate_with_fallback(text: str, src: str, dest: str) -> str | None:
    src = normalize_lang(src)
    dest = normalize_lang(dest)
    result = _get_provider_chain().translate(text, src, dest)
    _log_translate_event(src, dest, text, result)
    return result
```

Keep `_cached_translate()` unchanged so only non-`None` results enter diskcache.

- [ ] **Step 5: Delete obsolete scraper regression tests**

Delete `tests/test_try_google_retry.py` and `tests/test_error_page_rejection.py`. Their concerns are replaced by `test_invalid_azure_response_returns_none`, provider fallback tests, and the no-Google dependency verification in Task 6.

- [ ] **Step 6: Run focused translator regression tests**

Run:

```bash
pytest tests/test_translate_log.py tests/test_translate_cache.py tests/test_multiline_translation.py tests/test_mention_protection.py tests/test_has_translatable_content.py -v
```

Expected: all tests pass and no test requires a real provider call.

- [ ] **Step 7: Run the complete suite and commit**

Run:

```bash
pytest -v
```

Expected: all tests pass.

Commit:

```bash
git add translator.py tests/test_translate_log.py tests/test_try_google_retry.py tests/test_error_page_rejection.py
git commit -m "refactor: replace Google scraper with provider chain"
```

---

### Task 4: Runtime Dependencies, Compose Service, and Deployment Uploads

**Files:**
- Modify: `requirements.txt`
- Modify: `docker-compose.yml`
- Modify: `deploy.sh:19-20`
- Modify: `.env.example`

**Interfaces:**
- Consumes: `translation_providers.py`; official LibreTranslate v1.9.6 image and its `/languages` and `/translate` endpoints.
- Produces: internal hostname `http://libretranslate:5000`, named volume `libretranslate_models`, and bot environment variables listed in Global Constraints.

- [ ] **Step 1: Write a Compose/config regression test**

Create `tests/test_deployment_config.py`:

```python
from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]


def test_libretranslate_is_internal_pinned_and_persistent():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    service = compose["services"]["libretranslate"]
    assert service["image"] == "libretranslate/libretranslate:v1.9.6"
    assert "ports" not in service
    assert service["environment"]["LT_LOAD_ONLY"] == "en,es,fr,ja,ko,pl,ru,th,zt"
    assert str(service["environment"]["LT_THREADS"]) == "2"
    assert "libretranslate_models:/home/libretranslate/.local:rw" in service["volumes"]
    assert "healthcheck" in service
    assert "libretranslate_models" in compose["volumes"]


def test_provider_module_is_mounted_and_deployed():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    mounts = compose["services"]["discord-trans-bot"]["volumes"]
    assert "./translation_providers.py:/app/translation_providers.py:ro" in mounts
    deploy = (ROOT / "deploy.sh").read_text()
    assert "translation_providers.py" in deploy


def test_runtime_dependency_uses_requests_not_deep_translator():
    requirements = (ROOT / "requirements.txt").read_text().lower()
    assert "requests>=" in requirements
    assert "deep-translator" not in requirements
```

Add `PyYAML>=6.0` to `requirements-dev.txt`, not runtime requirements, so the deployment-config test can parse Compose.

- [ ] **Step 2: Install the new development dependency and verify failure**

Run:

```bash
pip install -r requirements-dev.txt
pytest tests/test_deployment_config.py -v
```

Expected: tests fail because the LibreTranslate service, mount, upload entry, and dependency change do not exist.

- [ ] **Step 3: Update runtime dependencies**

Change `requirements.txt` to:

```text
discord.py>=2.3.2
requests>=2.32.0
python-dotenv>=1.0.0
aiohttp>=3.9.0
diskcache>=5.6.0
```

Change `requirements-dev.txt` to:

```text
-r requirements.txt
pytest>=8.0.0
PyYAML>=6.0
```

- [ ] **Step 4: Add the internal LibreTranslate service and model volume**

Update `docker-compose.yml` so the bot mounts `translation_providers.py`, then add:

```yaml
  libretranslate:
    image: libretranslate/libretranslate:v1.9.6
    restart: unless-stopped
    environment:
      LT_LOAD_ONLY: en,es,fr,ja,ko,pl,ru,th,zt
      LT_THREADS: "2"
    volumes:
      - libretranslate_models:/home/libretranslate/.local:rw
    healthcheck:
      test: ["CMD-SHELL", "./venv/bin/python scripts/healthcheck.py"]
      interval: 15s
      timeout: 5s
      retries: 12
      start_period: 180s

volumes:
  libretranslate_models:
```

Do not add `depends_on` to the bot: Azure-only startup must remain possible while models are downloading or LibreTranslate is unhealthy.

- [ ] **Step 5: Update upload list and environment example**

Change `deploy.sh` to:

```bash
CODE_FILES=(bot.py translator.py translation_providers.py config.py glossary.py)
DEP_FILES=(docker-compose.yml Dockerfile requirements.txt)
```

Add this provider section to `.env.example` immediately after `DISCORD_TOKEN`, using only a placeholder key:

```dotenv
# 翻譯供應商：Azure 為主，NAS LibreTranslate 為備援
TRANSLATION_PROVIDER_ORDER=azure,libretranslate
AZURE_TRANSLATOR_KEY=your_azure_translator_key_here
AZURE_TRANSLATOR_REGION=eastasia
AZURE_TRANSLATOR_ENDPOINT=https://api.cognitive.microsofttranslator.com
AZURE_TRANSLATOR_TIMEOUT_SECONDS=5
LIBRETRANSLATE_URL=http://libretranslate:5000
LIBRETRANSLATE_TIMEOUT_SECONDS=30
AZURE_CIRCUIT_FAILURE_THRESHOLD=3
AZURE_CIRCUIT_COOLDOWN_SECONDS=300
LIBRETRANSLATE_MAX_CONCURRENCY=2
```

- [ ] **Step 6: Validate deployment configuration and run tests**

Run:

```bash
docker compose config -q
pytest tests/test_deployment_config.py tests/test_translation_providers.py -v
```

Expected: Compose exits 0 and all focused tests pass.

- [ ] **Step 7: Commit runtime deployment changes**

```bash
git add requirements.txt requirements-dev.txt docker-compose.yml deploy.sh .env.example tests/test_deployment_config.py
git commit -m "deploy: add private LibreTranslate fallback service"
```

---

### Task 5: Update Operator and Maintainer Documentation

**Files:**
- Modify: `DEPLOY.md`
- Modify: `README.md`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: final environment names, Docker service name, health check, provider event fields, circuit values, and language mappings from Tasks 1-4.
- Produces: exact NAS setup, key rotation, troubleshooting, and maintainer guidance.

- [ ] **Step 1: Update `DEPLOY.md` with the two-stage deployment procedure**

Add sections that specify:

1. Put the real Key 1 value only in `/volume1/docker/discord-trans-bot/.env` under `AZURE_TRANSLATOR_KEY`; use `eastasia` and the global text endpoint shown in Global Constraints.
2. Run `./deploy.sh --with-deps`, then rebuild the `discord-trans-bot` image in Container Manager because runtime dependencies changed.
3. Start LibreTranslate and explain that the first model download can take several minutes while its health status is `starting`.
4. Verify from NAS SSH without exposing port 5000:

```bash
cd /volume1/docker/discord-trans-bot
sudo docker compose ps
sudo docker compose exec discord-trans-bot python -c "import requests; print([x['code'] for x in requests.get('http://libretranslate:5000/languages', timeout=10).json()])"
```

5. Verify Azure through a Discord test message and inspect sanitized provider events:

```bash
python3 -c 'import json; p="/volume1/docker/discord-trans-bot/data/bot_log.json"; rows=json.load(open(p)); print(*[({k:r.get(k) for k in ("time","provider","success","latency_ms","fallback_reason","circuit_state")}) for r in rows if r.get("type")=="translate_provider"][-10:], sep="\n")'
```

6. Test fallback by temporarily replacing the NAS `.env` key with the literal non-secret value `invalid-test-key`, recreating only the bot container, sending one uncached Discord phrase, confirming `provider=libretranslate`, restoring Key 1, and recreating the bot container again. Warn not to paste the real key into terminal command arguments or shell history.
7. Rotate keys without downtime by moving the bot from Key 1 to Key 2, recreating the bot, verifying Azure success, and only then regenerating Key 1 in the portal.

- [ ] **Step 2: Update `README.md` user-facing backend description**

Add a short operational note: Azure is the normal high-quality provider; the NAS automatically falls back to local LibreTranslate/Argos during Azure outages or quota/connection failures; command behavior does not change; local fallback quality may be lower for non-English pairs because it can pivot through English.

- [ ] **Step 3: Replace Google scraper guidance in `CLAUDE.md`**

Remove the active `_UARequestsProxy`, `/m`, Google error-page, and `_try_google` retry descriptions. Preserve the incident history in one short paragraph explaining why Google must not return to the runtime chain.

Document these invariants explicitly:

- `translator.py` owns transformation/cache; `translation_providers.py` owns external translation.
- `bot.py` must not import provider implementations.
- `zh-TW` maps to Azure `zh-Hant` and LibreTranslate `zt` only at provider boundaries.
- Equal non-empty output is provider success.
- Breaker state is in-memory, thread-safe, and permits one half-open probe.
- LibreTranslate concurrency is two and its port is not published.
- Provider errors are sanitized; keys and webhook URLs are forbidden in logs.
- Source watcher and deploy list must include every mounted Python source file.

- [ ] **Step 4: Review documentation for secrets and stale provider statements**

Run:

```bash
rg -n "deep.translator|GoogleTranslator|translate\.google\.com/m|_try_google|your_azure_translator_key_here|AZURE_TRANSLATOR_KEY" README.md DEPLOY.md CLAUDE.md .env.example
```

Expected: Google terms appear only in historical explanation; the Azure key appears only as an environment variable name or the exact placeholder `your_azure_translator_key_here`; no real key-like value appears.

- [ ] **Step 5: Commit documentation**

```bash
git add DEPLOY.md README.md CLAUDE.md
git commit -m "docs: document Azure and LibreTranslate operations"
```

---

### Task 6: Full Verification and NAS Rollout

**Files:**
- Verify: all tracked project files
- Modify on NAS only: `/volume1/docker/discord-trans-bot/.env` with the user-entered secret

**Interfaces:**
- Consumes: all Tasks 1-5, the user-controlled Azure Key 1 clipboard value, SSH target `jim@192.168.1.11`, NAS project path `/volume1/docker/discord-trans-bot`.
- Produces: passing local suite, rebuilt NAS services, verified Azure primary path, verified local fallback path, and secret-free logs.

- [ ] **Step 1: Run static secret and Google-runtime scans**

Run:

```bash
git grep -n -E "Ocp-Apim-Subscription-Key|AZURE_TRANSLATOR_KEY"
git grep -n -E "deep_translator|GoogleTranslator|translate\.google\.com/m|_try_google"
```

Expected: the first scan contains only header construction, environment lookups, placeholders, tests, and docs; the second contains only historical docs/spec/plan text, with no Python runtime import or call.

- [ ] **Step 2: Run formatting-independent source validation and full tests**

Run:

```bash
python -m py_compile bot.py translator.py translation_providers.py config.py glossary.py
pytest -v
docker compose config -q
```

Expected: every command exits 0 and all tests pass.

- [ ] **Step 3: Review the complete branch diff**

Run:

```bash
git status --short
git diff HEAD~5 --stat
git diff HEAD~5 -- translator.py translation_providers.py docker-compose.yml deploy.sh requirements.txt .env.example
```

Expected: only planned files changed; `.DS_Store` remains untracked and is not staged; no secret appears.

- [ ] **Step 4: Upload code and dependency files**

Run:

```bash
./deploy.sh --with-deps
```

Expected: `bot.py`, `translator.py`, `translation_providers.py`, `config.py`, `glossary.py`, `docker-compose.yml`, `Dockerfile`, and `requirements.txt` report successful upload.

- [ ] **Step 5: Have the user place Key 1 into the NAS `.env`**

In Synology File Station, open `/volume1/docker/discord-trans-bot/.env`, add the exact provider block from `.env.example`, replace only `your_azure_translator_key_here` with the copied Key 1 value, and save. Do not paste the key into chat or a shell command. Confirm `.env` is not part of the deployment upload list and remains absent from `git status`.

- [ ] **Step 6: Rebuild and start the Compose project**

In DSM Container Manager, open project `discord-trans-bot`, choose **Build/Rebuild**, and start the project. Rebuilding is mandatory because `deep-translator` is removed and `requests` becomes an explicit runtime dependency.

From SSH, verify:

```bash
cd /volume1/docker/discord-trans-bot
sudo docker compose ps
```

Expected: `discord-trans-bot` is running; LibreTranslate progresses from `starting` to `healthy` after model download.

- [ ] **Step 7: Verify fallback model availability and direct local translations**

Run from the bot container so port 5000 remains private:

```bash
sudo docker compose exec discord-trans-bot python -c "import requests; u='http://libretranslate:5000'; print(sorted(x['code'] for x in requests.get(u+'/languages',timeout=10).json()))"
sudo docker compose exec discord-trans-bot python -c "import requests; u='http://libretranslate:5000/translate'; print(requests.post(u,json={'q':'你好','source':'zt','target':'en','format':'text'},timeout=30).json()['translatedText'])"
sudo docker compose exec discord-trans-bot python -c "import requests; u='http://libretranslate:5000/translate'; print(requests.post(u,json={'q':'hello','source':'en','target':'zt','format':'text'},timeout=30).json()['translatedText'])"
sudo docker compose exec discord-trans-bot python -c "import requests; u='http://libretranslate:5000/translate'; print(requests.post(u,json={'q':'bonjour','source':'fr','target':'ja','format':'text'},timeout=60).json()['translatedText'])"
```

Expected: language output contains `en es fr ja ko pl ru th zt`; each translation returns a non-empty string; the French-to-Japanese call demonstrates English pivoting.

- [ ] **Step 8: Verify Azure primary through Discord and structured logs**

Send a new, unique phrase in one configured Discord language channel so diskcache cannot satisfy it. Query the last provider events with the sanitized log command from Task 5.

Expected: at least one event has `provider: azure`, `success: true`, a non-negative `latency_ms`, and no key/header value.

- [ ] **Step 9: Verify automatic fallback without disclosing the real key**

Follow the documented controlled invalid-key procedure: edit only the NAS `.env` value to `invalid-test-key`, recreate the bot container, send a second unique phrase, and inspect provider events.

Expected: Azure logs `success: false` with `fallback_reason: http_401` or `http_403`; LibreTranslate logs `success: true`; the translated Discord message is delivered. Restore the real Key 1 immediately and recreate the bot container.

- [ ] **Step 10: Confirm recovery and absence of Google traffic**

Send a third unique phrase after restoring Key 1. Inspect recent logs:

```bash
python3 -c 'import json; p="/volume1/docker/discord-trans-bot/data/bot_log.json"; rows=json.load(open(p)); print(*[r for r in rows[-200:] if r.get("type")=="translate_provider"], sep="\n")'
```

Expected: Azure succeeds again; no event/message mentions Google `/m`, CAPTCHA, `TooManyRequests`, or `deep-translator`; no event contains `Ocp-Apim-Subscription-Key` or the real key.

- [ ] **Step 11: Record final verification evidence**

Capture in the task handoff:

- Local pytest pass count and duration.
- `docker compose config -q` success.
- NAS service states.
- The set of nine LibreTranslate language codes.
- One Azure success event and one LibreTranslate fallback success event with only timestamp/provider/status/latency/reason fields.
- Confirmation that Key 1 was restored and never printed.

Do not create an extra commit for production logs or secrets; they remain outside Git.
