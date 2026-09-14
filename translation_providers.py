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
