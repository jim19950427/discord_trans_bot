# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Discord bot that bridges multiple language channels: a message sent in one language channel is translated and re-posted (via webhook, impersonating the original author's name/avatar) into every other language channel in the same group. See README.md for the full user-facing command/feature reference and DEPLOY.md for NAS deployment steps.

## Commands

```bash
# Install
pip install -r requirements-dev.txt   # includes requirements.txt + pytest

# Run the bot locally (needs DISCORD_TOKEN in .env)
python bot.py

# Run all tests
pytest

# Run a single test file / test
pytest tests/test_multiline_translation.py
pytest tests/test_multiline_translation.py::test_single_line_message_unaffected -v

# Deploy to the NAS (hot-reload — see "Deployment" below)
./deploy.sh                 # code only
./deploy.sh --with-deps     # also push docker-compose.yml/Dockerfile/requirements.txt
```

`conftest.py` puts the repo root on `sys.path` so tests can import modules directly without a package structure. Provider tests must mock HTTP; check what a test mocks before assuming it is network-free.

## Architecture

### Module split

- **`bot.py`** — all Discord wiring: event handlers (`on_message`, `on_raw_message_edit/delete`, reactions, pins, threads), slash/prefix commands, and the webhook forward/edit/delete plumbing. This is the biggest file and owns almost all state.
- **`translator.py`** — text transformation and cache: `translate_many_with_status()` handles destination-keyed batch work with automatic source detection; `translate_text_with_status()` and `translate_text()` preserve single-target compatibility. `get_translation_status()` exposes provider health. No Discord objects ever reach this module — only plain strings, language codes, and plain dicts (glossary/substitutions).
- **`config.py`** — persists `channel_configs` (which channels are language channels, per guild) to `CONFIG_FILE`.
- **`glossary.py`** — persists everything else that needs to survive a restart: glossary terms, pre-translation substitutions, per-user language preferences, message clusters, thread-to-thread mappings, and pinned-message caches. All of it is guild/user-scoped JSON keyed by stringified IDs (JSON object keys are always strings; loaders convert back to `int`).

### The message cluster (`_msg_clusters` in `bot.py`)

Every source message that gets forwarded produces one shared `cluster` dict, stored under **every** resulting message ID across every channel (source + all mirrors) as keys pointing to the *same* dict object. This is what makes edit/delete/reaction/pin sync possible: given any one copy's message ID, the cluster tells you every other copy's ID and channel. Consequences to keep in mind when touching this code:

- Mutating a cluster field mutates it for every channel that shares it — there's no per-channel copy.
- Any handler that reacts to an event on one copy (edit, delete, pin, reaction) must decide what to do about the *other* copies still referencing the same cluster, and must be careful about `await` points that let a concurrent event for a sibling ID observe a half-updated cluster (see `on_raw_message_delete`'s ordering — sibling keys are cleared *before* the cross-channel delete `await`, specifically because the bot's own deletes re-trigger this same event handler).
- **Long posts are split, never truncated.** Discord rejects webhook content over 2000 characters, and translation can expand text (zh→ru/th) past it. `_split_message` breaks at a newline, then a space, then anywhere, budgets non-BMP characters (emoji) as two units, and closes/reopens a ``` code block across parts. `cluster["channels"][ch]` is always the *first* part; later parts' ids live in `cluster["extra_parts"][ch]` and are registered in `_msg_clusters` too, so delete/reply on any part resolves the cluster. Iterate with `_cluster_messages(cluster)` (not `cluster["channels"]`) wherever *every* message must be touched (delete, eviction, store); reactions/pins deliberately act on the first part only. `_send_with_parts` sends files with the first part and keeps what was sent if a continuation fails; `_edit_message_parts` grows/shrinks the continuation messages when an edit changes the part count (retries and re-translate go through it too) and keeps `extra_parts` equal to what actually exists even when it raises. `cluster["contents"]` always holds the full text. Pending retries persist `extra_ids` for the same reason as `prefix`: a retry restored after a restart has no cluster, and without them it would edit only the first part and orphan the others.
- Capped at `MAX_CLUSTER_ENTRIES` message ids (env var, default 2000 — one forwarded message takes one id per channel copy, so 9 channels ≈ 9 ids). Once exceeded, `_evict_clusters` drops whole clusters oldest-first down to 90% of the cap, so a message is either fully tracked or forgotten, never half-tracked; an edit/delete of an evicted message is ignored with an `Edit ignored ... has no cluster` log line. Persisted to `msg_clusters.json` every 60s (`_persist_clusters` loop) and on shutdown (`on_close`), then restored on `on_ready`.
- **File format v2** stores each cluster once with the message ids (`keys`) that point at it, and `load_clusters` returns every id of a message mapped to the *same* dict again (the pre-v2 file held one full copy per id and loaded as independent dicts, so after a restart an edit through one copy never reached the others; identical legacy copies are re-shared on load, so the first save migrates the file). The persist loop serializes on the event loop and only writes in a thread (the cluster dicts are shared and mutable), and wraps each file in try/except — an unhandled exception would stop the `tasks.loop` for good and silently lose clusters. Code older than v2 cannot read the new file: its loader raises an uncaught `TypeError`, so `on_ready` aborts partway and the bot half-starts (no clusters, no persist/heartbeat loops). Before any downgrade, restore the pre-migration copy of `msg_clusters.json` (back it up before the first v2 deploy).

### Forwarding must never let one channel's failure take down the rest

`on_message`'s `asyncio.gather(*tasks, return_exceptions=True)` and the `try/except` inside `_send_pretranslated`/`_raw_forward_send` around `webhook.send()` exist because of a real incident: those `webhook.send()` calls used to be unguarded, so any exception (oversized attachment, a transient Discord API error) propagated through a bare `gather()`, which aborted the whole message's forward *before* the cluster-building code ran — silently dropping delivery to every target channel, including ones whose send had already succeeded in the background, with zero trace in `bot_log.jsonl` (discord.py's default error handler only prints to stderr, which nothing here captures). Diagnosed by finding a message with fully successful translate log entries but no `_msg_clusters` entry anywhere. Any new code path that calls `webhook.send()`/`webhook.edit_message()` needs the same per-call try/except — don't rely on the caller's `gather()` to contain a failure.

**Never log `webhook_url`** — it embeds the webhook's auth token. Log the channel ID or author instead.

`_retry_missing_channels` (bot.py) recovers from exactly this failure mode after the fact: given a cluster, it forwards the source message to any group channel absent from `cluster["channels"]`, using the url/filename pairs the cluster already persists for attachments (`_ClusterAttachment`, a minimal stand-in for `discord.Attachment` — no need for the original `discord.Message`). It's wired into both the 🔄 reaction handler and the "重新翻譯" context menu, so triggering either on *any* copy of a message also backfills channels that never received it. Translation work is batched and routed by destination language. The persisted `raw_forward` flag preserves the `\` prefix's translation bypass after restart; historical records without the flag default to `False` because their original mode cannot be reconstructed safely. Attachment-only retries also bypass translation. Known gap: backfills drop reply-quote headers rather than risk double-`"> "`-quoting — `cluster["prefixes"][ch_id]` stores an already-`"> "`-formatted block, while `_send_pretranslated` expects raw quote text.

### Shared translation pipeline (`translator.py`)

Order matters and each stage exists because of a specific failure mode observed in production:

1. **Strip stray control characters** (`_STRAY_LINEBREAK_RE`) and normalize `\r\n`/`\r` → `\n`. Copy-pasted text can carry invisible characters (`\x1d` etc.) that Discord never renders but that `str.splitlines()` would treat as line breaks — always split on `"\n"` explicitly, never `.splitlines()`.
2. **Extract Discord mentions** (`<@id>`, `<@&id>`, `<#id>`) before anything else touches the text — substitutions/glossary/translation have no concept of mention syntax and can corrupt the numeric ID if a term happens to match a substring of it.
3. **Apply pre-translation substitutions** (`/addsub`) — plain, unbounded `re.sub`, no word-boundary protection. (This is why step 2 has to happen first.)
4. **Extract custom Discord emoji** (`<:name:id>`) before sending text to a provider.
5. **Multi-line fast path**: if the message has multiple lines and *no line* needs special handling (a real glossary match for the current dest language, or a Unicode emoji/URL mixed inline with real words), the whole block is translated in one API call so cross-line context survives. A glossary merely *existing* for the guild must not disable this — only an actual match on a given line should (see `_line_matches_glossary`). Otherwise falls back to per-line translation with per-line glossary/emoji/URL handling.
6. Glossary terms become `§N§` placeholders before translation and are restored after (`_apply_glossary`/`_restore_glossary`). ASCII terms get word-boundary matching; non-ASCII (CJK) terms match as plain substrings since CJK has no whitespace boundaries. A `"*"` translation target means "keep as original text in every language" (proper nouns).
7. Successful non-cached translations go through `diskcache` (`TRANSLATE_CACHE_DIR`, persists across restarts) keyed on `(text, src, dest)`.

### Translation providers

`translator.py` owns text transformation and cache; `translation_providers.py` owns all external translation. `bot.py` must not import provider implementations. Azure is primary; internal LibreTranslate is the fallback, guarded by its own circuit breaker (outage-type failures only: request errors, invalid JSON, HTTP 5xx) so a cold-starting service fails a whole batch fast instead of costing a timeout per language; HTTP calls reuse a per-thread `requests.Session`. `zh-TW` maps only at provider boundaries: Azure `zh-Hant`, LibreTranslate `zt`. Equal non-empty output is provider success. The in-memory breaker is thread-safe and permits one half-open probe; LibreTranslate concurrency is two and its port is never published. Provider errors are sanitized: keys and webhook URLs are forbidden in logs. Every mounted Python source file must be present in both the source watcher and deploy list.

Batch fan-out groups compatible destination targets into one provider call: one Azure batch is one circuit result, and fallback receives only unresolved targets. Provider aliases sharing an Azure wire code use one `to` parameter and populate every requested key; duplicate response entries leave that wire code unresolved. Partial Azure responses record the sanitized fallback reason `partial_response`. Channel configuration is destination-only; user input remains `auto` source. Azure languages are extensible through channel configuration, but every language expected to work during NAS fallback must also be added to `LT_LOAD_ONLY` in `docker-compose.yml`, followed by recreation of the LibreTranslate service/container to apply its environment.

Azure `type="translate_provider"` events include `target_count`, the number of distinct canonical target keys in that batch (not channel count; aliases still count as separate keys). Success, failure, missing-key, and open-circuit events all include it; skipped events do not imply an HTTP request. For NAS acceptance, send fresh uncached text to `en`/`ja`/`ko` destinations with compatible inputs and expect one Azure success event with `target_count=3`. Use the allowlisted inspection query in `DEPLOY.md`; do not print message, translation, credential, header, or webhook fields.

`/translation-status` requires Manage Channels and always defers/follows up ephemerally. It uses only `translator.get_translation_status(probe_libre=True)` and `_format_translation_status()` must explicitly render its three approved health fields; never serialize arbitrary status keys or show credentials, webhook URLs, message content, or translations.

Channel language settings are destination languages only, so users may type any language in any configured channel. User-authored messages, edits, thread names, retries, and context-menu translations request source `auto`; `translator.py` changes the provider source to `zh-TW` only when the text mixes Latin and Han characters without Japanese kana or Korean Hangul. Provider boundaries then map it to Azure `zh-Hant` or LibreTranslate `zt`. A destination matching this inferred source is rendered locally instead of sent to a provider. A successful non-empty result equal to the input is valid when the provider detected the target language (for example English detected while targeting English). LibreTranslate equal-output responses detected as another language are `unchanged_response` failures: forward the original immediately, then retry once after 60 seconds.

Google's previous mobile-page scraper failed behind CAPTCHA/rate limits and could cache error pages. Keep that history only as a reason never to restore it to the runtime chain.

`has_translatable_content()` mirrors `translate_text`'s own "nothing to actually translate" checks (mentions/custom-emoji/Unicode-emoji/URL-only content), so those messages do not enter retry scheduling. For real translation attempts, `translate_text_with_status()` carries explicit provider success separately from text; never infer failure by comparing output with input.

### Structured logging (`log_event` in `translator.py`)

`log_event(message, **fields)` is the one logging entry point for the whole bot — it prints (for the DSM/container log viewer) *and* appends a JSON entry to a shared, size-capped JSON-lines log file (one entry per line) (`BOT_LOG_FILE`, default `/data/bot_log.jsonl`, capped at `BOT_LOG_MAX_ENTRIES` entries, oldest dropped first). `bot.py` calls it everywhere it used to call `print()`. `_log_translate_event` is a thin wrapper that also records `src`/`dest`/`input`/`output` fields for translate calls specifically (`type: "translate"` vs the default `type: "info"`). When debugging a production issue, this file is more useful than the DSM log viewer — pull it via SSH and query it with `jq`/Python rather than scrolling through Container Manager's log UI.

### Background work, recovery and liveness (`bot.py`)

- **Never bare `asyncio.create_task`.** asyncio keeps only a weak reference to running tasks, so a fire-and-forget task can be garbage-collected mid-sleep. Use `_spawn()`, which holds a strong reference and logs an exception on completion.
- **Delayed translation retries are persisted** (`_schedule_retry` → `pending_retries.json`, ids and text only — never webhook URLs). `os._exit` on deploy skips `on_close`, so every change is written immediately, and `_restore_pending_retries` re-arms them in `on_ready` (entries older than an hour are dropped). Entries also store the reply-quote `prefix`, because clusters are only saved every 60s and a restart inside the retry window usually finds no cluster on disk — `_run_retry` rebuilds a minimal cluster from the stored prefix instead of dropping the retry. It resolves the webhook URL and glossary at run time and drops the retry only if the channel is no longer configured. One pending entry per `channel:message` key (`_schedule_retry` ignores duplicates), since a second task's `finally` would delete the first's persisted entry.
- **`on_ready` fires again after a gateway reconnect.** The first-time restore (clusters, pins, retries, loops) is guarded by `_startup_done`; re-running it would replace live shared cluster dicts with an older disk snapshot.
- **Webhook sends go through `_webhook_send`**, which recreates a webhook deleted in Discord (JSON error code 10015 only — other 404s such as an unknown thread are not "missing webhook") and retries once. `discord.File` objects are single-use, so sends keep raw bytes and rebuild files per attempt. Healed URLs are remembered in `_healed_urls` so fan-outs holding a stale `channel_configs` snapshot still reach the new webhook. Edits/deletes of messages posted by the old webhook cannot be healed (a webhook only edits its own messages).
- **Edits/deletes that race the forward wait for it.** `on_message` marks each message in `_inflight_forwards` until `_forward_message` has stored the cluster; `on_raw_message_edit`/`on_raw_message_delete` wait (up to 60s) when the id has no cluster yet, so a typo fixed — or a message deleted — a second after sending still reaches every mirror.
- **Thread copies need `thread=` on webhook edits/deletes.** Discord looks for a webhook message in the parent channel unless the thread is named, so every edit/delete/resend passes `_thread_kwargs(_thread_of(cluster, ch_id))` (from `cluster["thread_channels"]`).
- **Updates to our own mirrors are skipped by id.** Discord sends a partial MESSAGE_UPDATE (no `webhook_id`/author) when it unfurls a link in a mirror, usually before the cluster is stored; `_webhook_send` records each posted id for 120s (`_note_own_message`) and `on_raw_message_edit` returns early for them, so they don't log as `Edit ignored` (which is reserved for untracked *user* messages).
- **Command errors:** a command with its own `.error` handler makes discord.py's default `CommandTree.on_error` return silently, so errors were swallowed with no reply or log. Do not add per-command `.error` handlers; `_on_app_command_error` / `on_command_error` reply with `_command_error_text` (never internals) and log `type="error"`.
- **One shared `aiohttp` session** (`_get_http_session()`) serves every webhook call and attachment download; do not open `aiohttp.ClientSession()` per call (it cost a TCP+TLS handshake per channel of a fan-out). It is bound to the loop that created it and re-created if closed or if the loop changed; `on_close` closes it.
- **Alerts:** `translator.log_event` calls a registered hook for every `type="error"` event; `bot.py` turns those (plus provider events whose circuit breaker is `open`, which are `translate_provider` events rather than errors, and a watchdog restart, via `RESTART_REASON_FILE`) into a message in `ALERT_CHANNEL_ID` when that env var is set. `_AlertLimiter` rate-limits per message prefix and per hour; text is truncated and webhook URLs redacted. Alert *delivery* failures must be logged as `info`, never `error`, or they would re-enter the hook.
- **Liveness:** the `_heartbeat` loop touches `HEARTBEAT_FILE` (mtime only, no fsync — the one deliberate exception to the atomic-write rule, since it holds no data) and a `_watchdog` thread started in `on_ready` calls `os._exit(1)` if the loop is silent for 5 minutes or the gateway is not ready for 15. Docker's `restart` policy does not act on an *unhealthy* container, only on exit, which is why the process restarts itself; the compose healthcheck reads the same file for visibility.

### Persistence rules

Every JSON data file is written through `config.atomic_write_json` (temp file → fsync → `os.replace`), because the hot-reload watcher's `os._exit(0)` can land mid-write and a truncated `msg_clusters.json` used to be loaded as `{}`. Never `open(path, "w")` a data file directly. Loaders move unparseable files aside (`quarantine_corrupt` → `*.corrupt-<timestamp>`) instead of letting the next save overwrite them. Cluster/thread/pin saves pass `skip_if_unchanged=True`, so the 60s persist loop only touches disk when content changed. Atomic replace is only safe for files in the `/data` directory mount — the source files are single-file bind mounts, where replacing the inode would leave the container on the old file; `deploy.sh` therefore stages `*.new`, verifies size, backs up each existing file to `*.bak`, then overwrites in place with `cat >` — and if any write fails it restores every file that differs from its backup and verifies each restore with `cksum` (exit 3), keeping all `*.bak` and exiting 4 if a restore itself fails, so the NAS is never left running a mix of old and new modules (`tests/test_deploy_script.py` exercises this against a fake `ssh`).

### Hot-reload deployment

`bot.py` starts a daemon thread (`_watch_source_files`) that polls every mounted Python source file and calls `os._exit(0)` (never `sys.exit`, which only unwinds the calling thread) when one changes. Docker's `restart: unless-stopped` policy then brings the container back up running the new code. `on_ready` also writes `/data/status.json` with a `last_start` timestamp for deployment verification. `./deploy.sh` is code-only and must not read or overwrite the NAS `.env`; reserve `--with-deps` for deployment-configuration/dependency changes. Recreating a container on the NAS needs `sudo /usr/local/bin/docker compose -p trans-bot up -d --force-recreate <service>`: `sudo`'s PATH lacks `/usr/local/bin`, and the live containers belong to the `trans-bot` compose project, so omitting `-p trans-bot` (the folder name would give `discord-trans-bot`) creates a second bot container on the same token that double-posts every message.
