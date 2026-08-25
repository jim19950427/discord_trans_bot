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

`conftest.py` puts the repo root on `sys.path` so tests can `import translator`/`import bot` directly without a package structure. Tests never hit the real Google Translate API for assertions on logic (they monkeypatch `translator._translate_with_fallback` / `_try_google` / `GoogleTranslator`), but nothing prevents a test from making a real network call if it doesn't mock — check what a test mocks before assuming it's network-free.

## Architecture

### Module split

- **`bot.py`** — all Discord wiring: event handlers (`on_message`, `on_raw_message_edit/delete`, reactions, pins, threads), slash/prefix commands, and the webhook forward/edit/delete plumbing. This is the biggest file and owns almost all state.
- **`translator.py`** — pure text transformation: `translate_text()` is the single entry point every caller in `bot.py` goes through. No Discord objects ever reach this module — only plain strings, language codes, and plain dicts (glossary/substitutions).
- **`config.py`** — persists `channel_configs` (which channels are language channels, per guild) to `CONFIG_FILE`.
- **`glossary.py`** — persists everything else that needs to survive a restart: glossary terms, pre-translation substitutions, per-user language preferences, message clusters, thread-to-thread mappings, and pinned-message caches. All of it is guild/user-scoped JSON keyed by stringified IDs (JSON object keys are always strings; loaders convert back to `int`).

### The message cluster (`_msg_clusters` in `bot.py`)

Every source message that gets forwarded produces one shared `cluster` dict, stored under **every** resulting message ID across every channel (source + all mirrors) as keys pointing to the *same* dict object. This is what makes edit/delete/reaction/pin sync possible: given any one copy's message ID, the cluster tells you every other copy's ID and channel. Consequences to keep in mind when touching this code:

- Mutating a cluster field mutates it for every channel that shares it — there's no per-channel copy.
- Any handler that reacts to an event on one copy (edit, delete, pin, reaction) must decide what to do about the *other* copies still referencing the same cluster, and must be careful about `await` points that let a concurrent event for a sibling ID observe a half-updated cluster (see `on_raw_message_delete`'s ordering — sibling keys are cleared *before* the cross-channel delete `await`, specifically because the bot's own deletes re-trigger this same event handler).
- Capped at `MAX_CLUSTER_ENTRIES` (env var, default 2000); oldest third evicted once exceeded. Persisted to `msg_clusters.json` every 60s (`_persist_clusters` loop) and on shutdown (`on_close`), then restored on `on_ready`.

### `translate_text()` pipeline (`translator.py`)

Order matters and each stage exists because of a specific failure mode observed in production:

1. **Strip stray control characters** (`_STRAY_LINEBREAK_RE`) and normalize `\r\n`/`\r` → `\n`. Copy-pasted text can carry invisible characters (`\x1d` etc.) that Discord never renders but that `str.splitlines()` would treat as line breaks — always split on `"\n"` explicitly, never `.splitlines()`.
2. **Extract Discord mentions** (`<@id>`, `<@&id>`, `<#id>`) before anything else touches the text — substitutions/glossary/translation have no concept of mention syntax and can corrupt the numeric ID if a term happens to match a substring of it.
3. **Apply pre-translation substitutions** (`/addsub`) — plain, unbounded `re.sub`, no word-boundary protection. (This is why step 2 has to happen first.)
4. **Extract custom Discord emoji** (`<:name:id>`) — Google Translate chokes on the syntax.
5. **Multi-line fast path**: if the message has multiple lines and *no line* needs special handling (a real glossary match for the current dest language, or a Unicode emoji/URL mixed inline with real words), the whole block is translated in one API call so cross-line context survives. A glossary merely *existing* for the guild must not disable this — only an actual match on a given line should (see `_line_matches_glossary`). Otherwise falls back to per-line translation with per-line glossary/emoji/URL handling.
6. Glossary terms become `§N§` placeholders before translation and are restored after (`_apply_glossary`/`_restore_glossary`). ASCII terms get word-boundary matching; non-ASCII (CJK) terms match as plain substrings since CJK has no whitespace boundaries. A `"*"` translation target means "keep as original text in every language" (proper nouns).
7. Successful non-cached translations go through `diskcache` (`TRANSLATE_CACHE_DIR`, persists across restarts) keyed on `(text, src, dest)`.

`_try_google` retries differently depending on *why* it didn't get a usable result: a genuine retryable exception (429/rate limit/quota) gets the full exponential backoff (up to 4 attempts, 2/4/8s); a result that merely *equals the input* is not a transient failure (it usually means the content has no different translation — timestamps, leftover glossary placeholders, decoratively-spaced text) and gets a much shorter budget instead. A result that looks like *Google's own error page* (`_looks_like_error_page`) is discarded outright rather than returned — see below.

### The scraper is not an API (`_UARequestsProxy` in `translator.py`)

`deep-translator` doesn't call an API; it scrapes the `translate.google.com/m` mobile page. Two consequences that have already bitten this bot in production and that any change here must preserve:

- **A browser `User-Agent` is mandatory.** `deep-translator` sends no UA, so `requests` defaults to `python-requests/x.y`. In Aug 2026 Google began answering that UA with **HTTP 200** whose result slot contains its own `Error 500 (Server Error)` page. Because the status code is 200, `deep-translator`'s `request_failed()` check passes and it happily scrapes the error text. `_UARequestsProxy` patches the `requests` reference inside `deep_translator.google` to inject a real browser UA — there is no supported hook for headers. Removing it silently breaks all translation.
- **Never trust a scraped result just because it differs from the input.** That was the only sanity check, and Google's error page passed it: the text got returned as a translation, posted to Discord, *and written to the persistent `diskcache`*, where it would be served forever with no API call and no retry. 86 cache entries had to be purged. `_looks_like_error_page` now rejects these before they can be returned or cached, and deliberately refuses to fire when the source text itself contained the marker phrase.

`has_translatable_content()` mirrors `translate_text`'s own "nothing to actually translate" checks (mentions/custom-emoji/Unicode-emoji/URL-only content) and exists specifically so callers (the retry-scheduling logic in `bot.py`) can tell "translation intentionally returned unchanged" apart from "translation actually failed" — don't let these two drift out of sync if either changes.

### Structured logging (`log_event` in `translator.py`)

`log_event(message, **fields)` is the one logging entry point for the whole bot — it prints (for the DSM/container log viewer) *and* appends a JSON entry to a shared, size-capped log file (`BOT_LOG_FILE`, default `/data/bot_log.json`, capped at `BOT_LOG_MAX_ENTRIES` entries, oldest dropped first). `bot.py` calls it everywhere it used to call `print()`. `_log_translate_event` is a thin wrapper that also records `src`/`dest`/`input`/`output` fields for translate calls specifically (`type: "translate"` vs the default `type: "info"`). When debugging a production issue, this file is more useful than the DSM log viewer — pull it via SSH and query it with `jq`/Python rather than scrolling through Container Manager's log UI.

### Hot-reload deployment

`bot.py` starts a daemon thread (`_watch_source_files`) that polls the mtimes of the four source files and calls `os._exit(0)` (never `sys.exit`, which only unwinds the calling thread) when one changes. Docker's `restart: unless-stopped` policy then brings the container back up running the new code — no manual restart needed after `./deploy.sh`. `on_ready` also writes `/data/status.json` with a `last_start` timestamp specifically so a deploy script can poll for it and confirm the restart actually happened, without needing `docker logs` (which requires `sudo` and isn't available non-interactively over SSH to the NAS).
