# Multi-Target Batch Translation and Provider Status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Translate one Discord message to all compatible destination languages with one Azure request and expose sanitized provider health through `/translation-status`.

**Architecture:** `translation_providers.py` gains a target-keyed batch interface using repeated Azure `to` parameters and unresolved-target LibreTranslate fallback. `translator.py` builds target-specific transformation plans, groups identical provider inputs, and preserves per-target cache/glossary behavior. `bot.py` translates before fan-out and routes results by language key, while a sanitized status façade feeds an administrator-only command.

**Tech Stack:** Python 3.11+, `requests`, `discord.py`, `diskcache`, `pytest`, Azure Translator REST v3, LibreTranslate v1.9.6.

**Spec:** `docs/superpowers/specs/2026-09-15-batch-translation-status-design.md`

## Global Constraints

- Channel language is destination-only; every user-authored input uses source `auto`.
- Preserve `translate_text()` and `translate_text_with_status()` compatibility.
- Batch results use canonical language-code keys, never channel or response position.
- Azure uses one request with repeated `to` values for compatible targets; source `auto` omits `from`.
- `zh-TW` maps to Azure `zh-Hant` and LibreTranslate `zt` only at provider boundaries.
- One Azure batch is one circuit attempt; keep 3 failures, 300-second cooldown, one half-open probe, and generation safety.
- Keep Azure timeout 5 seconds, Libre translation timeout 30 seconds, Libre health timeout 5 seconds, and Libre concurrency two.
- Only unresolved targets fall back to LibreTranslate.
- Equal non-empty output is success. Total provider failure retains original text with explicit failure status.
- Cache remains per provider-input text, source, and target; failures are never cached.
- `/translation-status` requires Manage Channels, is ephemeral, and makes no synthetic Azure translation.
- Logs, status, and command output exclude message text, translations, keys, headers, and webhook URLs.
- Do not introduce a destination-language allowlist. Future Azure codes pass through unchanged.
- No new runtime or Compose dependency.

## File Structure

- `translation_providers.py`: provider batch calls, circuit/fallback accounting, health state, Libre probe.
- `translator.py`: transformation plans, grouped batch execution, cache, status façade, single-target wrappers.
- `bot.py`: batch routing, webhook delivery, status command.
- `tests/test_translation_providers.py`: provider and health behavior.
- `tests/test_translate_many.py`: transformation/cache/glossary grouping.
- `tests/test_auto_source_routing.py`: Discord language-key routing.
- `tests/test_translation_status.py`: status formatting and command behavior.
- `README.md`, `DEPLOY.md`, `CLAUDE.md`: operation and maintenance guidance.

---

### Task 1: Provider Batch API and Partial Fallback

**Files:**
- Modify: `translation_providers.py`
- Modify: `tests/test_translation_providers.py`

**Interfaces:**
- Consumes: existing mappings, circuit breaker, Azure/Libre HTTP behavior.
- Produces: `TranslationProviderChain.translate_many(text: str, source: str, targets: list[str]) -> dict[str, str | None]`; `translate()` wraps one target.

- [ ] **Step 1: Write the failing Azure batch test**

```python
def test_azure_batch_uses_repeated_targets_and_response_language_keys():
    post = Mock(return_value=FakeResponse(payload=[{"translations": [
        {"text": "こんにちは", "to": "ja"},
        {"text": "你好", "to": "zh-Hant"},
        {"text": "Hallo", "to": "de"},
    ]}]))
    chain = TranslationProviderChain(
        make_settings(provider_order=("azure",)), post=post
    )
    result = chain.translate_many("hello", "auto", ["zh-TW", "de", "ja", "de"])
    assert result == {"zh-TW": "你好", "de": "Hallo", "ja": "こんにちは"}
    assert post.call_count == 1
    assert post.call_args.kwargs["params"] == [
        ("api-version", "3.0"), ("to", "zh-Hant"),
        ("to", "de"), ("to", "ja"),
    ]
```

- [ ] **Step 2: Verify RED**

Run `pytest tests/test_translation_providers.py::test_azure_batch_uses_repeated_targets_and_response_language_keys -v`.

Expected: FAIL because `translate_many` is absent.

- [ ] **Step 3: Implement target normalization and Azure parsing**

```python
def _unique_canonical_targets(targets):
    return list(dict.fromkeys(canonicalize_language(x) for x in targets))

def _azure_params(source, targets):
    params = [("api-version", "3.0")]
    params.extend(("to", map_language(x, "azure")) for x in targets)
    if source.lower() != "auto":
        params.append(("from", map_language(source, "azure")))
    return params
```

Add `_azure_translate_many(text, source, targets)`. Post one body item, validate HTTP/JSON as today, map each valid `translations[]` entry through its `to` field, and reverse-map Azure codes to canonical requested codes. Ignore malformed individual entries; raise `ProviderError("invalid_response")` only when no valid requested result remains. Unknown target codes pass through.

- [ ] **Step 4: Implement chain fallback**

Initialize `{target: None}` for every unique target. Admit Azure once, call it once, record one circuit outcome, then remove resolved targets. For provider order entries after Azure, call existing semaphore-protected Libre translation only for unresolved targets. Implement `translate()` as:

```python
def translate(self, text, source, target):
    target = canonicalize_language(target)
    return self.translate_many(text, source, [target])[target]
```

- [ ] **Step 5: Add partial and circuit tests**

Add literal tests proving: a reversed Azure response routes correctly; a partial `fr` response sends only missing `ja` to Libre; an eight-target Azure HTTP 500 increments `_circuit._failures` from 0 to 1; a missing key skips Azure once; stale successes cannot close an open circuit; Libre concurrency remains two; single-target tests still pass.

- [ ] **Step 6: Verify and commit**

```bash
pytest tests/test_translation_providers.py -v
git add translation_providers.py tests/test_translation_providers.py
git commit -m "feat: batch Azure translation targets"
```

---

### Task 2: Sanitized Health State and Libre Probe

**Files:**
- Modify: `translation_providers.py`
- Modify: `tests/test_translation_providers.py`

**Interfaces:**
- Consumes: provider attempts from Task 1.
- Produces: `status_snapshot() -> dict`; `probe_libretranslate(timeout: float = 5.0) -> dict`.

- [ ] **Step 1: Write failing health tests**

```python
def test_status_snapshot_is_sanitized_and_batch_aware():
    post = Mock(return_value=FakeResponse(payload=[{"translations": [
        {"text": "bonjour", "to": "fr"}, {"text": "こんにちは", "to": "ja"},
    ]}]))
    chain = TranslationProviderChain(
        make_settings(), post=post, wall_clock=lambda: 1234.0
    )
    chain.translate_many("private phrase", "auto", ["fr", "ja"])
    status = chain.status_snapshot()
    assert status["azure"]["last_success_at"] == 1234.0
    assert status["azure"]["last_target_count"] == 2
    assert status["azure"]["circuit_state"] == "closed"
    assert "private phrase" not in repr(status)
    assert "test-key" not in repr(status)
```

Add a probe test using injected `get` and a `/languages` payload for `en`, `ja`, `ko`, `zt`; assert URL, timeout `5.0`, sorted codes, health, and latency.

- [ ] **Step 2: Verify RED**

Run `pytest tests/test_translation_providers.py -k "status_snapshot or probe_libre" -v`.

Expected: FAIL because constructor injections and health methods are absent.

- [ ] **Step 3: Implement lock-protected allowlisted state**

Extend constructor with `get=requests.get` and `wall_clock=time.time`. Store only these fields for each provider: `last_attempt_at`, `last_success_at`, `last_failure_at`, `last_latency_ms`, `last_failure_reason`, `last_target_count`. Store fallback `last_at` and bounded `reason`. Add Azure `configured` and live `circuit_state` only when copying the snapshot. Record category names such as `http_429`, never exception messages.

- [ ] **Step 4: Implement active Libre health probe**

GET `{libretranslate_url}/languages` with the supplied timeout. A 2xx list with at least one string `code` returns:

```python
{"healthy": True, "languages": sorted_codes,
 "latency_ms": elapsed_ms, "failure_reason": None}
```

HTTP, invalid JSON/shape, and request errors return the same keys with `healthy=False`, an empty list, and a bounded category. The probe must not alter the Azure circuit or passive translation history.

- [ ] **Step 5: Verify and commit**

Test successful, 503, timeout, invalid payload, fallback timestamp/reason, and secret exclusion.

```bash
pytest tests/test_translation_providers.py -v
git add translation_providers.py tests/test_translation_providers.py
git commit -m "feat: expose sanitized provider health"
```

---

### Task 3: Transformation Plans, Grouping, and Cache

**Files:**
- Modify: `translator.py`
- Create: `tests/test_translate_many.py`
- Modify: `tests/test_translate_cache.py`
- Run unchanged: multiline, glossary, mention, emoji, and auto-source tests.

**Interfaces:**
- Consumes: provider `translate_many()` and current transformation pipeline.
- Produces: `_translate_many_with_source_status(text, source_lang, target_langs, glossary=None, substitutions=None, _use_cache=True) -> dict[str, TranslationOutcome]`; public `translate_many_with_status(text, target_langs, glossary=None, substitutions=None, _use_cache=True) -> dict[str, TranslationOutcome]` fixes source to `auto`; `get_translation_status(probe_libre=True) -> dict`.

- [ ] **Step 1: Write failing basic batch tests**

```python
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
```

Add a cache test preloading `("你好", "auto", "en")` and asserting the provider receives only `ja`.

- [ ] **Step 2: Verify RED**

Run `pytest tests/test_translate_many.py -v`.

Expected: FAIL because the batch translator is absent.

- [ ] **Step 3: Extract explicit plans**

Add `_SegmentPlan(original, provider_text, placeholder_map, urls, unicode_emojis)` and `_TargetPlan(target, segments, separator, mentions, custom_emojis, terminal_none=False)`. Extract `_build_target_plan()` from the existing `translate_text()` branches without changing literal behavior. Whole-block mode produces one segment; line mode preserves every line; passthrough and full-glossary segments have `provider_text=None`; partial glossary segments retain their target-specific placeholder maps.

Add `_render_target_plan(plan, segment_results)` to restore glossary values, URLs, emoji, custom emoji, mentions, line separators, and original text on missing provider output. Mark `provider_succeeded=False` if any required segment is unresolved; equal non-empty results remain successful.

- [ ] **Step 4: Group and execute provider jobs**

For each target/segment, check cache key `(provider_text, "auto", target)`. Put misses in an insertion-ordered mapping `provider_text -> [(target, segment_index)]`. For each provider text, call the chain once with unique target keys. Cache only non-empty successes, then render every target independently. Duplicate requested targets are removed with `dict.fromkeys` after canonicalization; there is no allowlist.

- [ ] **Step 5: Test target-specific glossary grouping**

Use `{"Jim": {"en": "James", "ja": "ジム"}}`; assert both targets share provider input `Hello §0§` and restore different target values. Then use an English-only glossary entry and assert calls separate into `("Hello §0§", ["en"])` and `("Hello Jim", ["ja"])`. Add multiline jobs with shared and different inputs, and a partial provider failure producing original text only for the failed target.

- [ ] **Step 6: Preserve single-target APIs and expose status façade**

Put grouping in `_translate_many_with_source_status()`, whose explicit `source_lang` becomes the cache source and provider source. Public `translate_many_with_status()` calls it with source `auto`. `translate_text_with_status()` calls the private executor with its caller-supplied source and one target, preserving explicit-source compatibility. `translate_text()` returns only `.text`.

Implement the status façade separately:

```python
def get_translation_status(*, probe_libre=True):
    chain = _get_provider_chain()
    result = chain.status_snapshot()
    result["libretranslate_probe"] = (
        chain.probe_libretranslate(timeout=5.0) if probe_libre else None
    )
    return result
```

- [ ] **Step 7: Verify and commit**

```bash
pytest tests/test_translate_many.py tests/test_translate_cache.py tests/test_multiline_translation.py tests/test_glossary_matching.py tests/test_mention_protection.py tests/test_auto_source_routing.py -v
pytest -q
git add translator.py tests/test_translate_many.py tests/test_translate_cache.py
git commit -m "feat: group multi-language translation work"
```

---

### Task 4: Discord Batch Routing

**Files:**
- Modify: `bot.py`
- Modify: `tests/test_auto_source_routing.py`

**Interfaces:**
- Consumes: target-keyed `translate_many_with_status()`.
- Produces: one translation batch before independent webhook fan-out.

- [ ] **Step 1: Write a failing routing-order test**

Configure source `zh-TW` and target channels ordered `ko`, `en`, `ja`. Return outcomes ordered `ja`, `en`, `ko`. Capture actual target/webhook/text delivery and assert Korean goes to `ko-hook`, unchanged English to `en-hook`, and Japanese to `ja-hook`; assert the batch translator was called once with targets `['ko', 'en', 'ja']` and source behavior remains automatic.

- [ ] **Step 2: Verify RED**

Run `pytest tests/test_auto_source_routing.py::test_on_message_batches_once_and_routes_by_language_key -v`.

Expected: FAIL because `on_message` still translates per target.

- [ ] **Step 3: Split delivery from translation**

Extract `_send_pretranslated(outcome, target, webhook_url, ...) -> _ForwardResult | None` using the existing attachment download, quote formatting, webhook exception containment, and explicit `translation_succeeded`. Keep `_translate_and_send()` as a one-target compatibility wrapper.

- [ ] **Step 4: Batch before fan-out**

Collect canonical target languages, call `translate_many_with_status()` once in `asyncio.to_thread`, and create send tasks using `outcomes[target_lang]`. Never zip result values to channels. Multiple channels with one target reuse one outcome. Raw-forward and attachment-only paths do not call translation.

- [ ] **Step 5: Cover all multi-target entry points**

Write one failing routing test, then batch each of: source-message edits, attachment-change resend, thread-name creation, `_retry_missing_channels`, and personal context-menu translation. Results must be keyed by language in every path. Keep `_retry_translate` and `_do_retranslate` single-target because they operate on exactly one destination.

- [ ] **Step 6: Verify and commit**

```bash
pytest tests/test_auto_source_routing.py -v
pytest -q
git add bot.py tests/test_auto_source_routing.py
git commit -m "feat: route Discord fan-out through batch translation"
```

---

### Task 5: `/translation-status`, Documentation, and NAS Acceptance

**Files:**
- Modify: `bot.py`
- Create: `tests/test_translation_status.py`
- Modify: `README.md`
- Modify: `DEPLOY.md`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: `get_translation_status(probe_libre=True)`.
- Produces: Manage-Channels-only ephemeral command and production evidence.

- [ ] **Step 1: Write failing status tests**

Create a literal fixture with Azure configured/closed, last success/latency/target count, empty fallback, healthy Libre probe, and an extra `unexpected_secret`. Assert `_format_translation_status(status).to_dict()` includes only allowlisted health fields and loaded codes, never the secret. Invoke `slash_translation_status.callback(fake_interaction)` and assert defer/send both use `ephemeral=True` and the command has a permission check.

- [ ] **Step 2: Verify RED**

Run `pytest tests/test_translation_status.py -v`.

Expected: FAIL because formatter and command are absent.

- [ ] **Step 3: Implement allowlisted formatting**

Create a `翻譯服務狀態` embed with exactly three fields: Azure configuration/circuit/last result/latency/target count; Libre probe/passive result/loaded languages; recent fallback time/reason. Use Discord relative timestamps for numeric epochs and `尚無資料` for missing history. Read known keys explicitly; never iterate arbitrary snapshot fields into output.

- [ ] **Step 4: Register the command**

```python
@bot.tree.command(name="translation-status", description="查看翻譯服務健康狀態")
@app_commands.checks.has_permissions(manage_channels=True)
async def slash_translation_status(interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        status = await asyncio.to_thread(get_translation_status, probe_libre=True)
        embed = _format_translation_status(status)
    except Exception:
        embed = discord.Embed(
            title="翻譯服務狀態",
            description="暫時無法取得狀態，翻譯服務不受此查詢影響。",
            color=discord.Color.orange(),
        )
    await interaction.followup.send(embed=embed, ephemeral=True)
```

Wire missing permission into the existing ephemeral permission handler. Test healthy, unhealthy, empty-history, façade exception, and injected key/webhook/message fields.

- [ ] **Step 5: Update documentation**

Document batch behavior, destination-only channels, extensible Azure languages, the status command, one-batch/one-circuit invariant, allowlist-only status output, code-only deployment, and the requirement to update `LT_LOAD_ONLY` when adding a Libre fallback language.

- [ ] **Step 6: Run final local verification**

```bash
pytest -q
git diff --check
python3 -m py_compile bot.py translator.py translation_providers.py
```

Expected: zero test failures, whitespace errors, and compile errors.

- [ ] **Step 7: Commit and deploy**

```bash
git add bot.py tests/test_translation_status.py README.md DEPLOY.md CLAUDE.md
git commit -m "feat: add translation provider status command"
./deploy.sh
```

The code-only deploy must not read or overwrite NAS `.env`.

- [ ] **Step 8: Verify NAS acceptance**

Confirm a fresh `status.json` startup time. Send a unique English message from a concrete `zh-TW` destination channel; verify unchanged English delivery plus Japanese/Korean translations. Sanitized provider logs must show one Azure event per compatible provider-input group with the correct target count. Invoke `/translation-status` as a Manage Channels administrator and verify ephemeral visibility, Azure configured/closed/recent success, healthy Libre `/languages`, and no message or credential data.

- [ ] **Step 9: Perform final whole-branch review**

Review the diff from the plan merge base against every spec section. Specifically verify glossary grouping, language-key routing, circuit accounting, unresolved-only fallback, equal-output status, command permission, and secret exclusion before claiming completion.
