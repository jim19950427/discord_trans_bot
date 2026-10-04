# Multi-Target Batch Translation and Provider Status Design

Date: 2026-09-15
Status: Approved in conversation; awaiting written-spec review

## Goal

Reduce redundant Azure requests and make translation health visible without weakening the bot's existing language flexibility, glossary behavior, cache semantics, or Azure-to-LibreTranslate failover.

The delivered change has two user-visible outcomes:

1. One message can be translated to multiple configured destination languages through one Azure request whenever their prepared provider input is compatible.
2. Administrators can run `/translation-status` to see sanitized Azure and LibreTranslate health information.

## Scope

### In scope

- Add multi-target interfaces in `translator.py` and `translation_providers.py`.
- Batch Azure targets using repeated `to` query parameters.
- Preserve automatic source-language detection for messages from every configured channel.
- Preserve per-target cache entries, glossary restoration, substitutions, emoji, URL, mention, and multiline behavior.
- Fall back only unresolved target languages to LibreTranslate.
- Track sanitized provider health in memory.
- Add an administrator-only, ephemeral `/translation-status` Discord command.
- Keep target-language handling open-ended so adding an Azure-supported language does not require editing a fixed target list.

### Out of scope

- Changing Azure subscription, pricing tier, or quota.
- Publishing the LibreTranslate port.
- Downloading additional Argos models automatically.
- Persistent monitoring, notifications, or scheduled health checks.
- Removing message text from the existing general translation log. That is a separate privacy change.
- Reworking the Discord channel/group configuration model.

## Existing Invariants

- A channel language is its destination language only. User-authored input always uses source `auto`.
- `translator.py` owns transformation and caching.
- `translation_providers.py` owns external HTTP translation.
- `bot.py` must not import concrete provider implementations.
- Azure is primary and NAS LibreTranslate is the fallback.
- Azure timeout remains 5 seconds; LibreTranslate translation timeout remains 30 seconds.
- The Azure circuit opens after three failed Azure requests for 300 seconds and permits one half-open probe.
- LibreTranslate translation concurrency remains two.
- Equal non-empty output is a successful provider result.
- Provider logs and status must never include translation text, Azure keys, or webhook URLs.

## Architecture

### Public translator interface

Add a target-keyed batch API:

```python
translate_many_with_status(
    text: str,
    target_langs: list[str],
    glossary: dict | None = None,
    substitutions: dict | None = None,
    _use_cache: bool = True,
) -> dict[str, TranslationOutcome]
```

Keys are canonical destination language codes, not positional indexes. Duplicate targets are removed while preserving first-seen order. The result contains one entry for every unique requested target, including failed targets whose existing fallback text is the original source.

Existing `translate_text()` and `translate_text_with_status()` remain available. They delegate to the same batch core with one target, so thread translation, manual retranslation, UI translation, and external callers remain compatible.

### Transformation plans and grouping

Each uncached target is compiled into a transformation plan using the current pipeline:

1. Normalize line endings and remove stray line-break controls.
2. Extract Discord mentions.
3. Apply source substitutions.
4. Extract custom emoji.
5. Select multiline fast path or per-line handling.
6. Extract URLs and Unicode emoji where required.
7. Apply target-specific glossary placeholders.

A plan contains passthrough content, provider-input segments, and target-specific restoration data. Provider-input jobs with identical source text are grouped. Each group calls the provider batch interface once with all destination languages that need that exact input. Different glossary preparation can therefore form separate groups without changing existing glossary meaning.

Post-processing remains target-specific: glossary replacements, URLs, emoji, mentions, blank lines, and formatting are restored independently for each destination.

### Cache behavior

The persistent cache remains target-specific and provider-independent. Before building network jobs, the batch core checks the existing key `(text, "auto", target)` for each target. Cache hits are returned immediately; only misses enter provider grouping. Successful results are written under their individual target keys. Provider failures are never cached.

Changing the number or ordering of destination channels does not invalidate existing cache entries.

## Provider Batch Interface

`TranslationProviderChain` gains a target-keyed method:

```python
translate_many(
    text: str,
    source: str,
    targets: list[str],
) -> dict[str, str | None]
```

`translate()` remains as a one-target compatibility wrapper.

### Azure behavior

- Canonical targets are mapped at the Azure boundary, including `zh-TW` to `zh-Hant`.
- Source `auto` omits the Azure `from` parameter.
- One request repeats the `to` parameter for all unique mapped targets.
- Results are associated using each Azure translation object's `to` field, then mapped back to the requested canonical target. Code must not depend on response array position.
- Unknown future target codes pass through unchanged; there is no hard-coded allowlist in the batching logic.
- Empty, missing, duplicate, or malformed response entries leave only the affected targets unresolved.
- A successful non-empty response equal to the source is retained as success.

### Circuit-breaker accounting

An Azure batch is one admitted request and therefore records at most one circuit success or failure. A batch containing eight targets must not add eight failures. Stale concurrent completions retain the existing generation-token protection and cannot close a newer open circuit.

### LibreTranslate fallback

After Azure processing, only unresolved targets continue through the provider order. LibreTranslate keeps its existing one-target `/translate` calls and global concurrency limit of two because its API/runtime does not need to share Azure's multi-target implementation.

Successful Azure targets are never repeated locally. If Azure is unavailable, every unresolved target may use LibreTranslate. If both providers fail, that target keeps the current original-text fallback and explicit failure status so delayed retry remains possible.

## Provider Health State

The process-wide provider chain maintains a lock-protected, in-memory sanitized snapshot containing:

- Azure configured/not configured.
- Azure circuit state.
- Azure last attempt, last success, and last failure timestamps.
- Azure last latency and last sanitized failure category.
- LibreTranslate last attempt, last success, and last failure timestamps.
- LibreTranslate last latency and last sanitized failure category.
- Last fallback timestamp and reason.

Batch metrics describe one HTTP request and may include target count and target language codes. They must not contain source text, output text, keys, request headers, URLs containing credentials, or Discord webhook URLs. Health history resets on process restart; persistence is not required.

## `/translation-status` Command

The slash command is restricted to users with Discord's Manage Channels permission and always responds ephemerally.

It obtains data through a sanitized function exposed by `translator.py`; `bot.py` does not reach into provider internals.

When invoked, it displays:

- Azure configuration state, circuit state, last result time, last latency, and sanitized failure category.
- Whether a fallback was recently used and why.
- LibreTranslate's passive last-result data.
- An active LibreTranslate `/languages` probe with a dedicated 5-second timeout, plus the loaded language codes when healthy.

The command does not make a synthetic Azure translation request and therefore does not consume translation quota. If no provider request has occurred since restart, the corresponding fields say that no data is available.

Probe failure affects the displayed health result but does not open the Azure circuit or alter translation fallback state.

## Extending Languages

No bot or Azure batching code contains a fixed destination-language list. Adding a new Azure-supported language requires only configuring a destination channel with its language code.

LibreTranslate support remains deployment-dependent. To provide NAS fallback for a new language, the operator must also add its Argos language code to `LT_LOAD_ONLY` and rebuild/recreate the LibreTranslate service. Provider-boundary aliases remain the only code mappings, such as `zh-TW` to Azure `zh-Hant` and LibreTranslate `zt`.

## Error Handling

- One malformed Azure target does not discard valid sibling translations.
- One Discord webhook failure does not affect other destination channels.
- Cache hits and provider successes can be mixed in one returned batch.
- Explicit per-target success status, never text equality, controls delayed retry.
- An exception from status probing produces an unavailable status response rather than failing the command.
- All error details are restricted to bounded category/type information.

## Testing

Tests must be written before implementation and cover:

- One Azure HTTP request with multiple repeated `to` parameters.
- Source auto-detection omitting `from`.
- Mapping `zh-TW` through `zh-Hant` and back to the canonical result key.
- Result association by response `to`, independent of response ordering.
- Duplicate-target removal and pass-through of a newly introduced language code.
- Per-target cache hits excluding only those targets from network work.
- Grouping compatible transformation plans and separating incompatible glossary plans.
- Target-specific glossary restoration after a shared provider call.
- One circuit failure per failed Azure batch.
- Partial Azure results falling back only unresolved targets.
- LibreTranslate concurrency remaining at two.
- Equal non-empty results retaining successful status.
- Discord routing by language key rather than positional order.
- `/translation-status` permission, ephemeral response, healthy/unhealthy Libre probe, empty-history state, and absence of secrets/message text.
- Compatibility of the existing single-target APIs and the complete existing test suite.

## Deployment and Acceptance

No new package or Compose dependency is required. Deploy the modified Python files with the existing code-only deployment script; the source watcher restarts the bot.

Acceptance criteria on NAS:

1. Every configured channel retains a concrete destination language rather than `auto`.
2. Sending English in the Chinese destination channel produces an unchanged English copy in the English channel and translated copies in Japanese, Korean, and other destination channels.
3. For an uncached message whose targets share provider input, sanitized logs show one Azure batch request rather than one Azure request per destination.
4. `/translation-status` is visible to a Manage Channels administrator, responds only to that user, reports Azure closed/healthy after a successful translation, and reports the current LibreTranslate `/languages` probe.
5. No status output or provider event exposes a key, webhook URL, source text, or translated text.
