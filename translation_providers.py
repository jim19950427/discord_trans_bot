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


def _unique_canonical_targets(targets: list[str]) -> list[str]:
    return list(dict.fromkeys(canonicalize_language(target) for target in targets))


def _azure_params(source: str, targets: list[str]) -> list[tuple[str, str]]:
    params = [("api-version", "3.0")]
    wire_targets = {}
    for target in targets:
        wire_code = map_language(target, "azure")
        wire_targets.setdefault(wire_code.lower(), wire_code)
    params.extend(("to", code) for code in wire_targets.values())
    if source.lower() != "auto":
        params.append(("from", map_language(source, "azure")))
    return params


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
    libretranslate_circuit_failure_threshold: int = 3
    libretranslate_circuit_cooldown: float = 30.0

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
            libretranslate_circuit_failure_threshold=int(
                os.getenv("LIBRETRANSLATE_CIRCUIT_FAILURE_THRESHOLD", "3")
            ),
            libretranslate_circuit_cooldown=float(
                os.getenv("LIBRETRANSLATE_CIRCUIT_COOLDOWN_SECONDS", "30")
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
        self._generation = 0

    @property
    def state(self) -> str:
        with self._lock:
            if self._probe_in_flight:
                return "half_open"
            if self._open_until > self._monotonic():
                return "open"
            return "closed"

    def allow_request(self) -> tuple[bool, str]:
        allowed, state, _ = self.admit_request()
        return allowed, state

    def admit_request(self) -> tuple[bool, str, int | None]:
        with self._lock:
            now = self._monotonic()
            if self._open_until <= 0:
                return True, "closed", self._generation
            if now < self._open_until:
                return False, "open", None
            if self._probe_in_flight:
                return False, "half_open", None
            self._probe_in_flight = True
            return True, "half_open", self._generation

    def record_success(self, generation: int | None = None) -> str | None:
        with self._lock:
            if generation is not None and generation != self._generation:
                return None
            changed = self._failures > 0 or self._open_until > 0 or self._probe_in_flight
            self._failures = 0
            self._open_until = 0.0
            self._probe_in_flight = False
            return "closed" if changed else None

    def record_failure(self, generation: int | None = None) -> str | None:
        with self._lock:
            if generation is not None and generation != self._generation:
                return None
            self._failures += 1
            if self._probe_in_flight or self._failures >= self.failure_threshold:
                self._open_until = self._monotonic() + self.cooldown
                self._probe_in_flight = False
                self._generation += 1
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
        post: Callable[..., Any] | None = None,
        get: Callable[..., Any] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        logger: Callable[..., None] | None = None,
    ):
        self.settings = settings
        # Default transport reuses one keep-alive requests.Session per thread
        # (Session isn't guaranteed thread-safe) instead of a fresh TCP/TLS
        # handshake on every call. Injected callables (tests) are used as-is.
        self._local = threading.local()
        self._post = post or self._session_post
        self._get = get or self._session_get
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._logger = logger
        self._circuit = CircuitBreaker(
            settings.circuit_failure_threshold,
            settings.circuit_cooldown,
            monotonic=monotonic,
        )
        self._libre_circuit = CircuitBreaker(
            settings.libretranslate_circuit_failure_threshold,
            settings.libretranslate_circuit_cooldown,
            monotonic=monotonic,
        )
        self._libre_slots = threading.BoundedSemaphore(
            settings.libretranslate_max_concurrency
        )
        self._diagnostic_lock = threading.Lock()
        self._missing_key_logged = False
        self._health_lock = threading.Lock()
        self._health = {
            provider: {
                "last_attempt_at": None,
                "last_success_at": None,
                "last_failure_at": None,
                "last_latency_ms": None,
                "last_failure_reason": None,
                "last_target_count": None,
            }
            for provider in SUPPORTED_PROVIDERS
        }
        self._fallback = {"last_at": None, "reason": None}

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._local.session = requests.Session()
        return session

    def _session_post(self, *args, **kwargs):
        return self._session().post(*args, **kwargs)

    def _session_get(self, *args, **kwargs):
        return self._session().get(*args, **kwargs)

    @staticmethod
    def _is_outage(category: str) -> bool:
        """Failures that mean the service is down/overloaded (worth tripping
        the breaker), as opposed to content problems like unchanged_response."""
        return (
            category in ("request_error", "invalid_json")
            or category.startswith("http_5")
        )

    @staticmethod
    def _bounded_reason(reason: str) -> str:
        return reason[:160]

    def _record_provider_attempt(
        self,
        provider: str,
        *,
        success: bool,
        latency_ms: int,
        target_count: int,
        failure_reason: str | None = None,
    ) -> None:
        now = self._wall_clock()
        with self._health_lock:
            state = self._health[provider]
            state["last_attempt_at"] = now
            state["last_latency_ms"] = latency_ms
            state["last_target_count"] = target_count
            if success:
                state["last_success_at"] = now
            else:
                state["last_failure_at"] = now
                state["last_failure_reason"] = self._bounded_reason(
                    failure_reason or "request_error"
                )

    def _record_fallback(self, reason: str) -> None:
        with self._health_lock:
            self._fallback = {
                "last_at": self._wall_clock(),
                "reason": self._bounded_reason(reason),
            }

    def status_snapshot(self) -> dict[str, dict[str, Any]]:
        with self._health_lock:
            snapshot = {
                provider: dict(state) for provider, state in self._health.items()
            }
            fallback = dict(self._fallback)
        snapshot["azure"]["configured"] = bool(self.settings.azure_key)
        snapshot["azure"]["circuit_state"] = self._circuit.state
        snapshot["libretranslate"]["circuit_state"] = self._libre_circuit.state
        snapshot["fallback"] = fallback
        return snapshot

    def probe_libretranslate(self, timeout: float = 5.0) -> dict[str, Any]:
        started = self._monotonic()
        try:
            response = self._get(
                f"{self.settings.libretranslate_url}/languages", timeout=timeout
            )
            if not 200 <= response.status_code < 300:
                raise ProviderError(f"http_{response.status_code}")
            try:
                payload = response.json()
            except (TypeError, ValueError) as exc:
                raise ProviderError("invalid_json", type(exc).__name__) from exc
            if not isinstance(payload, list):
                raise ProviderError("invalid_response")
            languages = sorted(
                entry["code"]
                for entry in payload
                if isinstance(entry, dict) and isinstance(entry.get("code"), str)
            )
            if not languages:
                raise ProviderError("invalid_response")
        except ProviderError as error:
            return {
                "healthy": False,
                "languages": [],
                "latency_ms": int((self._monotonic() - started) * 1000),
                "failure_reason": self._bounded_reason(error.category),
            }
        except Exception:
            return {
                "healthy": False,
                "languages": [],
                "latency_ms": int((self._monotonic() - started) * 1000),
                "failure_reason": "request_error",
            }
        return {
            "healthy": True,
            "languages": languages,
            "latency_ms": int((self._monotonic() - started) * 1000),
            "failure_reason": None,
        }

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

    def _azure_translate_many(
        self, text: str, source: str, targets: list[str]
    ) -> dict[str, str]:
        headers = {
            "Ocp-Apim-Subscription-Key": self.settings.azure_key,
            "Ocp-Apim-Subscription-Region": self.settings.azure_region,
            "Content-Type": "application/json",
            "X-ClientTraceId": str(uuid.uuid4()),
        }
        try:
            response = self._post(
                f"{self.settings.azure_endpoint}/translate",
                params=_azure_params(source, targets),
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
        try:
            translations = payload[0]["translations"]
        except (IndexError, KeyError, TypeError) as exc:
            raise ProviderError("invalid_response", type(exc).__name__) from exc
        if not isinstance(translations, list):
            raise ProviderError("invalid_response", type(translations).__name__)

        targets_by_azure_code: dict[str, list[str]] = {}
        for target in targets:
            wire_code = map_language(target, "azure").lower()
            targets_by_azure_code.setdefault(wire_code, []).append(target)
        results_by_code = {}
        seen_codes = set()
        for translation in translations:
            if not isinstance(translation, dict):
                continue
            translated_text = translation.get("text")
            response_target = translation.get("to")
            if not isinstance(response_target, str):
                continue
            wire_code = response_target.lower()
            if wire_code not in targets_by_azure_code:
                continue
            if wire_code in seen_codes:
                results_by_code.pop(wire_code, None)
                continue
            seen_codes.add(wire_code)
            if isinstance(translated_text, str) and translated_text.strip():
                results_by_code[wire_code] = translated_text
        results = {
            target: translated_text
            for wire_code, translated_text in results_by_code.items()
            for target in targets_by_azure_code[wire_code]
        }
        if not results:
            raise ProviderError("invalid_response")
        return results

    def _log_missing_key_once(self, source: str, targets: list[str]) -> None:
        with self._diagnostic_lock:
            if self._missing_key_logged:
                return
            self._missing_key_logged = True
        self._emit(
            "azure",
            source,
            ",".join(targets),
            success=False,
            latency_ms=0,
            target_count=len(targets),
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
        translated = _response_text(payload, "libretranslate")
        if source.lower() == "auto" and translated.strip() == text.strip():
            detected = payload.get("detectedLanguage")
            detected_code = (
                detected.get("language") if isinstance(detected, dict) else None
            )
            target_code = map_language(target, "libretranslate")
            if (
                not isinstance(detected_code, str)
                or detected_code.lower() != target_code.lower()
            ):
                raise ProviderError("unchanged_response")
        return translated

    def translate_many(
        self, text: str, source: str, targets: list[str]
    ) -> dict[str, str | None]:
        targets = _unique_canonical_targets(targets)
        results: dict[str, str | None] = {target: None for target in targets}
        unresolved = list(targets)

        for provider in self.settings.provider_order:
            if not unresolved:
                break
            if provider == "azure":
                if not self.settings.azure_key:
                    self._log_missing_key_once(source, unresolved)
                    self._record_fallback("missing_key")
                    continue
                allowed, state, generation = self._circuit.admit_request()
                if not allowed:
                    self._record_fallback("circuit_open")
                    self._emit(
                        "azure",
                        source,
                        ",".join(unresolved),
                        success=False,
                        latency_ms=0,
                        target_count=len(unresolved),
                        fallback_reason="circuit_open",
                        circuit_state=state,
                    )
                    continue
                started = self._monotonic()
                try:
                    azure_results = self._azure_translate_many(text, source, unresolved)
                except ProviderError as error:
                    latency_ms = int((self._monotonic() - started) * 1000)
                    transition = self._circuit.record_failure(generation)
                    self._record_provider_attempt(
                        "azure",
                        success=False,
                        latency_ms=latency_ms,
                        target_count=len(unresolved),
                        failure_reason=error.category,
                    )
                    self._record_fallback(error.category)
                    self._emit(
                        "azure",
                        source,
                        ",".join(unresolved),
                        success=False,
                        latency_ms=latency_ms,
                        fallback_reason=error.category,
                        target_count=len(unresolved),
                        error_detail=error.detail or None,
                        circuit_state=transition or self._circuit.state,
                    )
                    continue
                transition = self._circuit.record_success(generation)
                latency_ms = int((self._monotonic() - started) * 1000)
                self._record_provider_attempt(
                    "azure",
                    success=True,
                    latency_ms=latency_ms,
                    target_count=len(unresolved),
                )
                self._emit(
                    "azure",
                    source,
                    ",".join(unresolved),
                    success=True,
                    latency_ms=latency_ms,
                    target_count=len(unresolved),
                    fallback_reason=None,
                    circuit_state=transition or self._circuit.state,
                )
                results.update(azure_results)
                unresolved = [target for target in unresolved if target not in azure_results]
                if unresolved:
                    self._record_fallback("partial_response")
                continue

            if provider == "libretranslate":
                for target in list(unresolved):
                    allowed, libre_state, libre_generation = (
                        self._libre_circuit.admit_request()
                    )
                    if not allowed:
                        # Service is down/cold-starting: fail the rest of this
                        # batch immediately instead of waiting out a timeout
                        # per target. The bot's retry pass picks them up.
                        self._record_fallback("circuit_open")
                        self._emit(
                            "libretranslate",
                            source,
                            ",".join(unresolved),
                            success=False,
                            latency_ms=0,
                            target_count=len(unresolved),
                            fallback_reason="circuit_open",
                            circuit_state=libre_state,
                        )
                        break
                    started = self._monotonic()
                    try:
                        result = self._libretranslate_translate(text, source, target)
                    except ProviderError as error:
                        latency_ms = int((self._monotonic() - started) * 1000)
                        if self._is_outage(error.category):
                            self._libre_circuit.record_failure(libre_generation)
                        else:
                            self._libre_circuit.record_success(libre_generation)
                        self._record_provider_attempt(
                            "libretranslate",
                            success=False,
                            latency_ms=latency_ms,
                            target_count=1,
                            failure_reason=error.category,
                        )
                        self._record_fallback(error.category)
                        self._emit(
                            "libretranslate",
                            source,
                            target,
                            success=False,
                            latency_ms=latency_ms,
                            fallback_reason=error.category,
                            error_detail=error.detail or None,
                            circuit_state=self._libre_circuit.state,
                        )
                        continue
                    latency_ms = int((self._monotonic() - started) * 1000)
                    self._libre_circuit.record_success(libre_generation)
                    self._record_provider_attempt(
                        "libretranslate",
                        success=True,
                        latency_ms=latency_ms,
                        target_count=1,
                    )
                    self._emit(
                        "libretranslate",
                        source,
                        target,
                        success=True,
                        latency_ms=latency_ms,
                        fallback_reason=None,
                        circuit_state=self._libre_circuit.state,
                    )
                    results[target] = result
                    unresolved.remove(target)
        return results

    def translate(self, text: str, source: str, target: str) -> str | None:
        target = canonicalize_language(target)
        return self.translate_many(text, source, [target])[target]
