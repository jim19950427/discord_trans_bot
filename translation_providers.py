from __future__ import annotations

import os
import threading
import time
import uuid
import warnings
from dataclasses import dataclass
from typing import Any, Callable

import requests


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

    def translate(self, text: str, source: str, target: str) -> str | None:
        for provider in self.settings.provider_order:
            if provider == "azure":
                if not self.settings.azure_key:
                    self._log_missing_key_once(source, target)
                    continue
                allowed, state = self._circuit.allow_request()
                if not allowed:
                    self._emit(
                        "azure",
                        source,
                        target,
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
                        "azure",
                        source,
                        target,
                        success=False,
                        latency_ms=int((self._monotonic() - started) * 1000),
                        fallback_reason=error.category,
                        error_detail=error.detail or None,
                        circuit_state=transition or self._circuit.state,
                    )
                    continue
                transition = self._circuit.record_success()
                self._emit(
                    "azure",
                    source,
                    target,
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
                        "libretranslate",
                        source,
                        target,
                        success=False,
                        latency_ms=int((self._monotonic() - started) * 1000),
                        fallback_reason=error.category,
                        error_detail=error.detail or None,
                        circuit_state=self._circuit.state,
                    )
                    continue
                self._emit(
                    "libretranslate",
                    source,
                    target,
                    success=True,
                    latency_ms=int((self._monotonic() - started) * 1000),
                    fallback_reason=None,
                    circuit_state=self._circuit.state,
                )
                return result
        return None
