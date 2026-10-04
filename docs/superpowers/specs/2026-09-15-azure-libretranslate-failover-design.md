# Azure Primary with LibreTranslate Failover Design

## Objective

Replace the unreliable Google Translate mobile-page scraper with a production translation path that uses Azure Translator as the primary provider and a self-hosted LibreTranslate/Argos service on the Synology NAS as the free fallback.

The Discord-facing behavior must remain unchanged: messages continue to pass through the existing mention, emoji, URL, substitution, glossary, multiline, cache, forwarding, retry, and message-cluster flows.

## Scope

This change includes:

- Azure Translator REST integration using the existing F0 resource in `East Asia`.
- A LibreTranslate container in the existing Docker Compose project.
- Provider selection, language-code mapping, timeouts, fallback, and an in-memory Azure circuit breaker.
- Provider-aware structured logging without credential disclosure.
- Unit tests for provider behavior and regression coverage for the existing text pipeline.
- NAS deployment documentation and post-deployment smoke tests.
- Removal of the Google scraper and the `deep-translator` dependency from the runtime path.

This change does not include:

- Custom Azure Translator models.
- A public LibreTranslate endpoint or LibreTranslate API keys.
- A GPU deployment.
- Per-guild provider selection.
- A new Discord command or user-facing provider indicator.
- Automatic Azure cost-tier upgrades.

## Current Context

`translator.py` owns all plain-text transformation and exposes `translate_text()` and `translate_text_nocache()` to `bot.py`. Calls run in worker threads through `asyncio.to_thread()`. The persistent `diskcache` is keyed by `(text, source language, destination language)`. `bot.py` treats a returned source string as degraded delivery and may schedule a delayed retry.

The current provider is `deep-translator`, which scrapes `translate.google.com/m`. Production logs showed persistent HTTP 429 responses and Google's anti-automation page. The scraper must not remain in the active or tertiary provider chain.

The NAS is a Synology DS920+ with an Intel Celeron J4125 and 19 GiB RAM. Its configured language channels currently use:

- `en`
- `es`
- `fr`
- `ja`
- `ko`
- `pl`
- `ru`
- `th`
- `zh-TW`

## Architecture

The translation flow is:

```text
Discord event
  -> existing text preparation, substitutions, and glossary placeholders
  -> persistent translation cache
  -> provider chain
       1. Azure Translator
       2. LibreTranslate on the Docker-internal network
       3. unchanged source text
  -> existing placeholder, URL, emoji, mention, and multiline restoration
  -> existing Discord forwarding and retry flow
```

`bot.py` continues to call the same public functions. Provider-specific behavior is isolated in a new `translation_providers.py` module. `translator.py` remains responsible for transformation and caching and delegates only the actual machine-translation request.

LibreTranslate runs as a separate service in the existing `docker-compose.yml`. It has no host `ports` mapping, so it is reachable by the bot at `http://libretranslate:5000` but is not exposed to the NAS LAN or the internet. A named Docker volume persists downloaded Argos models across restarts.

## Provider Interface

`translation_providers.py` will expose one provider-chain entry point with the conceptual contract:

```python
translate_with_providers(text: str, source: str, target: str) -> str | None
```

It returns the first usable provider result or `None` when every configured provider fails. The module owns:

- Environment-based provider order.
- Azure and LibreTranslate HTTP clients.
- Language-code mapping.
- Request timeouts and response validation.
- Azure circuit-breaker state.
- A process-wide LibreTranslate concurrency limit.
- Provider attempt logging.

The existing private function `_translate_with_fallback()` remains as the seam used by `translator.py` and existing tests, but its implementation delegates to the new provider chain. This minimizes changes to the text pipeline and keeps current monkeypatch-based tests useful.

## Configuration

The bot container receives these environment variables from the existing `.env` file:

```dotenv
TRANSLATION_PROVIDER_ORDER=azure,libretranslate
AZURE_TRANSLATOR_KEY=<secret Key 1 value>
AZURE_TRANSLATOR_REGION=eastasia
AZURE_TRANSLATOR_ENDPOINT=https://api.cognitive.microsofttranslator.com
AZURE_TRANSLATOR_TIMEOUT_SECONDS=5
LIBRETRANSLATE_URL=http://libretranslate:5000
LIBRETRANSLATE_TIMEOUT_SECONDS=30
AZURE_CIRCUIT_FAILURE_THRESHOLD=3
AZURE_CIRCUIT_COOLDOWN_SECONDS=300
LIBRETRANSLATE_MAX_CONCURRENCY=2
```

The Azure key is stored only in the NAS `.env`, which remains outside version control. It must never be placed in source code, documentation examples with a real value, logs, test fixtures, shell history, or chat messages.

If `AZURE_TRANSLATOR_KEY` is absent, Azure is skipped with one clear startup/runtime diagnostic and LibreTranslate remains usable. Unknown entries in `TRANSLATION_PROVIDER_ORDER` are ignored with a warning. If no valid provider is configured, translation degrades to the existing original-text behavior.

LibreTranslate uses:

```yaml
LT_LOAD_ONLY: en,es,fr,ja,ko,pl,ru,th,zt
LT_THREADS: 2
```

The image is pinned to `libretranslate/libretranslate:v1.9.6`, the current official stable release at design time, rather than `latest`. The model volume maps to `/home/libretranslate/.local`, the current official image user's model directory. A health check must verify that the HTTP service is ready before reporting healthy, but bot startup must not be blocked indefinitely when the fallback is unavailable.

## Language Mapping

The public bot configuration keeps its current language codes. Each provider maps only at its boundary:

| Bot code | Azure code | LibreTranslate/Argos code |
|---|---|---|
| `zh-TW` | `zh-Hant` | `zt` |
| `en` | `en` | `en` |
| `es` | `es` | `es` |
| `fr` | `fr` | `fr` |
| `ja` | `ja` | `ja` |
| `ko` | `ko` | `ko` |
| `pl` | `pl` | `pl` |
| `ru` | `ru` | `ru` |
| `th` | `th` | `th` |

Azure automatic detection is requested by omitting the `from` query parameter. LibreTranslate automatic detection uses `source=auto` only when the source passed to the provider chain is `auto`.

Argos models are loaded for both directions between English and each other configured language. When no direct non-English pair exists, Argos may pivot through English. This fallback prioritizes availability and privacy over Azure-equivalent quality.

## Azure Request Behavior

Azure uses the Text Translation REST API v3 endpoint:

```text
POST {AZURE_TRANSLATOR_ENDPOINT}/translate?api-version=3.0&from={source}&to={target}
```

The `from` parameter is omitted for automatic detection. Requests include:

- `Ocp-Apim-Subscription-Key`
- `Ocp-Apim-Subscription-Region`
- `Content-Type: application/json`
- A generated client trace ID

The request body is a one-element JSON array containing the text. A usable response must be HTTP 2xx JSON with a non-empty first translation text field. A valid result equal to the source text counts as a successful provider response; it does not indicate an Azure outage and must not trip the circuit breaker or invoke LibreTranslate solely for that reason.

## LibreTranslate Request Behavior

LibreTranslate uses its internal Docker service endpoint:

```text
POST http://libretranslate:5000/translate
```

The JSON request includes `q`, `source`, `target`, and `format: "text"`. A usable response must be HTTP 2xx JSON containing non-empty `translatedText`.

A process-wide bounded semaphore permits at most two concurrent LibreTranslate calls. Additional calls wait for a slot within the existing worker threads. This prevents a fallback burst across eight destination languages from overwhelming the NAS CPU. The HTTP timeout remains longer than Azure's because local CPU inference is expected to be slower.

## Failure and Circuit-Breaker Policy

Any Azure network error, timeout, HTTP non-2xx response, malformed response, or empty translation is a failed provider attempt and allows the same request to continue to LibreTranslate.

The Azure circuit breaker is process-local and thread-safe:

1. It starts closed.
2. Each Azure operational failure increments the consecutive-failure count.
3. Any usable Azure result resets the count to zero.
4. At three consecutive failures, the circuit opens for 300 seconds.
5. While open, new translations skip Azure and go directly to LibreTranslate.
6. After the cooldown, one request becomes the half-open Azure probe. Other concurrent requests continue to LibreTranslate rather than creating a probe burst.
7. A successful probe closes the circuit. A failed probe reopens it for another cooldown.

The breaker state intentionally resets when the bot process restarts. Persisting it would add stale-state risk without meaningful benefit.

LibreTranslate failures never prevent Discord forwarding. When both providers fail, the provider chain returns `None`; `translator.py` logs the all-providers failure and preserves its current original-text degradation. Existing delayed retry and manual retranslation behavior remain in place.

## Cache Semantics

The existing cache key `(text, source, destination)` remains unchanged. Translation output is valuable regardless of which provider produced it, so provider identity is not part of the key.

Only usable non-`None` provider results are cached. Provider error payloads, HTML, empty strings, and exceptions are never cached. `translate_text_nocache()` bypasses the cache but still uses the configured provider order and circuit-breaker state.

## Logging and Security

Every provider attempt emits a structured event with:

- `type: "translate_provider"`
- `provider`
- `src`
- `dest`
- `success`
- `latency_ms`
- A bounded error category or `fallback_reason`
- Azure circuit state when it changes

The existing final translation event remains available for production diagnosis. Logs must never include:

- Azure keys or authorization headers.
- Discord webhook URLs.
- Full HTTP response bodies from unexpected errors.
- LibreTranslate model download URLs containing future credentials, if any.

Exception logging uses the exception class and a sanitized, length-bounded message. Azure 401/403 events identify configuration/authentication failure without printing request headers.

## Deployment

Deployment proceeds in two separately verifiable stages:

1. Add and start LibreTranslate with its named model volume. Wait for all nine configured language codes to appear from its language endpoint, then smoke-test at least `zh-TW -> en`, `en -> zh-TW`, and one non-English pair that pivots through English.
2. Add the Azure variables to the NAS `.env`, deploy the provider code and bot dependency changes, rebuild the bot image, and verify an Azure-backed translation.

The bot must remain runnable when LibreTranslate is starting or unhealthy. Docker Compose may express a health check, but it must not create an indefinite hard dependency that prevents the Discord bot from starting with Azure alone.

Rollback consists of deploying the previous bot image/configuration. No user data migration is involved. The LibreTranslate volume can remain without affecting the old bot. The Google scraper is not retained as a rollback provider; restoring it would require an explicit source rollback.

## Testing

All automated provider tests use mocked HTTP responses and clocks; they must not call Azure, Google, or the NAS.

Required tests cover:

- Azure request URL, headers, region, body, and each supported language mapping.
- Omission of Azure `from` during automatic detection.
- Successful Azure response, including output equal to input.
- Azure timeout, connection failure, 429, 401/403, 5xx, invalid JSON, empty JSON, and structurally invalid JSON falling through to LibreTranslate.
- Successful LibreTranslate fallback and `zh-TW <-> zt` mapping.
- Both providers failing and returning `None`.
- Provider order controlled by environment configuration.
- Missing Azure key skipping Azure without leaking configuration.
- Circuit opening at three consecutive failures, skipping during cooldown, one half-open probe under concurrency, closing on success, and reopening on failure.
- LibreTranslate concurrency never exceeding two calls.
- Failed/error responses never entering `diskcache`.
- Existing multiline, glossary, substitutions, mention, emoji, URL, cache, log, and Discord forwarding regression tests.

Post-deployment verification covers:

1. LibreTranslate health and language availability.
2. A normal Discord translation whose provider log is `azure`.
3. A controlled Azure-authentication failure that produces a `libretranslate` result without exposing the test key or error response.
4. Restoration of the correct key followed by Azure circuit recovery after the cooldown or process restart.
5. Confirmation that no new Google `/m`, Google CAPTCHA, or `deep-translator` events appear in logs.

## Documentation Changes

- Update `.env.example` with placeholder provider settings and comments.
- Update `DEPLOY.md` with Azure secret placement, LibreTranslate model startup, health verification, image rebuild, failover smoke test, and key-rotation instructions.
- Update `CLAUDE.md` to replace the Google scraper operational notes with the provider-chain invariants, language mappings, circuit-breaker behavior, and secret-handling rules.
- Update `README.md` only where the user-facing translation backend description is currently inaccurate; no command behavior changes are planned.

## Acceptance Criteria

- A normal cache miss uses Azure Translator when Azure is configured, healthy, and its circuit is closed, then posts the translated message through the unchanged Discord flow.
- An Azure operational failure automatically uses local LibreTranslate without user action.
- Three consecutive Azure failures open the circuit for five minutes; recovery is automatic and concurrency-safe.
- LibreTranslate is reachable only through the Compose network and retains models across container restarts.
- All nine configured languages are available in the fallback service, with Traditional Chinese mapped to `zt`.
- Failure of both providers degrades to original text without dropping other destination-channel sends.
- No Google scraper request remains in the runtime path.
- No secret is committed, logged, or printed.
- All existing and new tests pass.

## References

- [Azure Translator REST usage](https://learn.microsoft.com/azure/ai-services/translator/text-translation/how-to/use-rest-api)
- [Azure Translator authentication](https://learn.microsoft.com/azure/ai-services/translator/text-translation/reference/authentication)
- [LibreTranslate official Docker Compose](https://github.com/LibreTranslate/LibreTranslate/blob/main/docker-compose.yml)
- [LibreTranslate v1.9.6 release](https://github.com/LibreTranslate/LibreTranslate/releases/tag/v1.9.6)
- [LibreTranslate installation and `--load-only`](https://github.com/LibreTranslate/Documentation/blob/main/src/content/docs/guides/installation.md)
- [Argos package index](https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json)
