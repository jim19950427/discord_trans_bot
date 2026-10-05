import os
import re
import json
import time
import threading
from dataclasses import dataclass
import diskcache
from config import atomic_write_text
from translation_providers import (
    ProviderSettings,
    TranslationProviderChain,
    canonicalize_language,
)

CACHE_DIR = os.getenv("TRANSLATE_CACHE_DIR", "/data/translate_cache")
CACHE_SIZE_LIMIT = int(os.getenv("TRANSLATE_CACHE_SIZE_LIMIT", str(50 * 1024 * 1024)))

LOG_FILE = os.getenv("BOT_LOG_FILE", "/data/bot_log.jsonl")
LOG_MAX_ENTRIES = int(os.getenv("BOT_LOG_MAX_ENTRIES", "5000"))
_log_lock = threading.Lock()
# Lines currently in each log file (path -> count), so appends stay O(1).
_log_counts: dict[str, int] = {}

_translate_cache: diskcache.Cache | None = None
_provider_chain: TranslationProviderChain | None = None
_provider_chain_lock = threading.Lock()


@dataclass(frozen=True)
class TranslationOutcome:
    text: str | None
    provider_succeeded: bool


@dataclass
class _SegmentPlan:
    original: str
    provider_text: str | None
    placeholder_map: dict[str, str]
    urls: list[str]
    unicode_emojis: list[str]


@dataclass
class _TargetPlan:
    target: str
    segments: list[_SegmentPlan]
    separator: str
    mentions: list[str]
    custom_emojis: list[str]
    terminal_none: bool = False


def _get_translate_cache() -> diskcache.Cache:
    """Lazily create the on-disk translation cache (avoids touching disk at import time)."""
    global _translate_cache
    if _translate_cache is None:
        _translate_cache = diskcache.Cache(
            CACHE_DIR,
            size_limit=CACHE_SIZE_LIMIT,
            eviction_policy="least-recently-used",
        )
    return _translate_cache

# Discord custom emoji: <:name:id> or animated <a:name:id>
_CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")
# Discord mentions: <@id>, <@!id> (nickname), <@&id> (role), <#id> (channel).
# Must be extracted before substitutions/glossary/translation touch the text —
# none of those steps understand mention syntax, and a substitution or
# glossary term that happens to be a substring of the numeric ID corrupts it.
_MENTION_RE = re.compile(r"<@[!&]?\d+>|<#\d+>")
# Unicode emoji ranges (covers the vast majority of emoji in common use)
_UNICODE_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF"  # Mahjong–Symbols and Pictographs Extended-A
    "\U00002600-\U000027BF"   # Misc symbols, dingbats
    "\U0000FE00-\U0000FEFF"   # Variation selectors
    "]+",
    re.UNICODE,
)
# URLs are preserved verbatim rather than sent to the translation provider.
_URL_RE = re.compile(r"https?://\S+")
# Matches any real word character (letters/digits from any script, incl. CJK)
_HAS_WORD_RE = re.compile(r"\w", re.UNICODE)
# Script hints for short mixed-language messages where provider auto-detection
# can over-weight Latin words and leave embedded Chinese untranslated.
_HAN_RE = re.compile("[\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U0002fa1f]")
_LATIN_RE = re.compile("[A-Za-z]")
_JAPANESE_KANA_RE = re.compile("[\u3040-\u30ff\u31f0-\u31ff]")
_HANGUL_RE = re.compile("[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]")
# Characters str.splitlines() treats as line boundaries but that Discord
# never renders as visible line breaks — typically invisible copy-paste
# artifacts (e.g. \x1d Group Separator) from other apps. Left in place,
# they fragment a message into spurious per-word "lines" during translation.
_STRAY_LINEBREAK_RE = re.compile("[\v\f\x1c\x1d\x1e\x85\u2028\u2029]")
def normalize_lang(code: str) -> str:
    return canonicalize_language(code)


def _provider_source_language(text: str, requested_source: str) -> str:
    if requested_source != "auto":
        return requested_source
    if (
        _HAN_RE.search(text)
        and _LATIN_RE.search(text)
        and not _JAPANESE_KANA_RE.search(text)
        and not _HANGUL_RE.search(text)
    ):
        return "zh-TW"
    return requested_source


def has_translatable_content(text: str) -> bool:
    """True if translate_text has anything to actually send to the
    translation engine once Discord mentions, custom emoji, Unicode emoji,
    and URLs are stripped out. A message that's purely made of those
    intentionally comes back unchanged from translate_text — callers must
    not treat that as a translation failure and retry it."""
    text = _MENTION_RE.sub("", text)
    text = _CUSTOM_EMOJI_RE.sub("", text)
    text = _UNICODE_EMOJI_RE.sub("", text)
    text = _URL_RE.sub("", text).strip()
    if not text:
        return False
    return bool(_HAS_WORD_RE.search(text))


def _init_log_count(path: str) -> int:
    """Count existing lines once per path. If a crash left the last line
    unterminated, close it so the next append doesn't fuse two entries.
    A legacy JSON-array log is moved aside to <path>.legacy."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        return 0
    if data.lstrip().startswith(b"["):
        # Legacy single JSON-array log (pre-JSONL, e.g. BOT_LOG_FILE still
        # pointing at bot_log.json): keep it aside rather than mixing formats.
        os.replace(path, path + ".legacy")
        return 0
    if data and not data.endswith(b"\n"):
        with open(path, "ab") as f:
            f.write(b"\n")
        data += b"\n"
    return data.count(b"\n")


def _trim_log(path: str) -> int:
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()[-LOG_MAX_ENTRIES:]
    atomic_write_text(path, "".join(lines))
    return len(lines)


_error_hook = None


def set_error_hook(hook) -> None:
    """Register `hook(message, fields)` to be called (from any thread) for every
    logged type="error" event and every provider event with an open circuit
    breaker; `fields["type"]` tells them apart. Used by the bot for alerts."""
    global _error_hook
    _error_hook = hook


def log_event(message: str, **fields) -> None:
    """Print message to the console (unchanged, still visible in the DSM log
    viewer) and also append a structured entry to a shared JSON-lines log file
    (one JSON object per line), trimmed to the newest LOG_MAX_ENTRIES entries
    once it grows 10% past the cap. Appending is O(1) — the file is never
    re-read or rewritten per event. Thread-safe — bot events and translate
    calls both come from concurrent worker threads.
    """
    print(message)
    entry = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "type": fields.pop("type", "info"),
        "message": message,
        **fields,
    }
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with _log_lock:
        try:
            path = LOG_FILE
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            count = _log_counts.get(path)
            if count is None:
                count = _init_log_count(path)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
            count += 1
            if count > LOG_MAX_ENTRIES + max(1, LOG_MAX_ENTRIES // 10):
                count = _trim_log(path)
            _log_counts[path] = count
        except OSError as e:
            print(f"[log write failed] {e}")
    # Provider outages are logged as translate_provider events (not errors):
    # the breaker tripping is what an operator needs to hear about.
    alertable = entry["type"] == "error" or (
        entry["type"] == "translate_provider" and fields.get("circuit_state") == "open"
    )
    if alertable and _error_hook is not None:
        try:
            _error_hook(message, {**fields, "type": entry["type"]})
        except Exception as e:  # an alert problem must never break logging
            print(f"[error hook failed] {e}")


def _get_provider_chain() -> TranslationProviderChain:
    global _provider_chain
    if _provider_chain is None:
        with _provider_chain_lock:
            if _provider_chain is None:
                _provider_chain = TranslationProviderChain(
                    ProviderSettings.from_env(), logger=log_event
                )
    return _provider_chain


def _log_translate_event(src: str, dest: str, text: str, result: str | None) -> None:
    message = f"[translate] ({src}->{dest}) {repr(text)} -> {repr(result)}"
    log_event(message, type="translate", src=src, dest=dest, input=text, output=result)


def _translate_with_fallback(text: str, src: str, dest: str) -> str | None:
    src = normalize_lang(src)
    dest = normalize_lang(dest)
    chain = _get_provider_chain()
    # The chain's scalar API already delegates to translate_many. Preserve it
    # for legacy integrations, while also accepting batch-only chains.
    if hasattr(chain, "translate"):
        result = chain.translate(text, src, dest)
    else:
        result = chain.translate_many(text, src, [dest]).get(dest)
    _log_translate_event(src, dest, text, result)
    return result


def _cached_translate(text: str, src: str, dest: str, cache: diskcache.Cache | None = None) -> str | None:
    if cache is None:
        cache = _get_translate_cache()

    key = (text, src, dest)
    if key in cache:
        return cache[key]

    result = _translate_with_fallback(text, src, dest)

    if result:
        cache[key] = result
    return result


def _term_pattern(term: str) -> re.Pattern:
    """Case-insensitive pattern for a glossary term.

    ASCII terms get word-boundary lookaround so "JIM" doesn't match inside
    "JIMMY". Terms containing non-ASCII characters (CJK or mixed) are matched
    as plain substrings, since CJK text has no whitespace word boundaries.
    """
    escaped = re.escape(term)
    if term.isascii():
        escaped = r"(?<![A-Za-z0-9])" + escaped + r"(?![A-Za-z0-9])"
    return re.compile(escaped, re.IGNORECASE)


def _apply_glossary(text: str, dest: str, glossary: dict) -> tuple[str, dict[str, str]]:
    """Replace source terms with §N§ placeholders so they survive translation.

    translations["*"] = original term means "keep as-is in all languages" (proper noun).
    """
    placeholder_map: dict[str, str] = {}
    for idx, (term, translations) in enumerate(glossary.items()):
        pattern = _term_pattern(term)
        if not pattern.search(text):
            continue
        if dest in translations:
            replacement = translations[dest]
        elif "*" in translations:
            replacement = translations["*"]
        else:
            continue
        ph = f"§{idx}§"
        text = pattern.sub(ph, text)
        placeholder_map[ph] = replacement
    return text, placeholder_map


def _restore_glossary(text: str, placeholder_map: dict[str, str]) -> str:
    for ph, target in placeholder_map.items():
        text = text.replace(ph, target)
    return text


def _build_target_plan(
    text: str,
    source_lang: str,
    target_lang: str,
    glossary: dict | None = None,
    substitutions: dict | None = None,
) -> _TargetPlan:
    src = normalize_lang(source_lang)
    dest = normalize_lang(target_lang)
    plan = _TargetPlan(dest, [], "\n", [], [])
    if src == dest:
        plan.terminal_none = True
        return plan

    # Normalize real line breaks, then strip stray control characters that
    # str.splitlines() would otherwise treat as line boundaries (see
    # _STRAY_LINEBREAK_RE comment) — these are invisible copy-paste
    # artifacts, not intentional line breaks, and must not fragment the
    # message into per-word "lines" below.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _STRAY_LINEBREAK_RE.sub("", text)

    # Pull out Discord mentions before anything else touches the text (see
    # _MENTION_RE comment for why).
    mentions = _MENTION_RE.findall(text)
    text = _MENTION_RE.sub("", text).strip()
    plan.mentions = mentions

    # Apply pre-translation substitutions (e.g. aliases / euphemisms)
    if substitutions:
        for src_term, replacement in substitutions.items():
            text = re.sub(re.escape(src_term), replacement, text, flags=re.IGNORECASE)

    # Pull out custom Discord emojis so translation providers never see their syntax.
    emojis = _CUSTOM_EMOJI_RE.findall(text)
    clean = _CUSTOM_EMOJI_RE.sub("", text).strip()

    if not clean:
        plan.terminal_none = not (emojis or mentions)
        plan.segments.append(_SegmentPlan(text, None, {}, [], []))
        return plan

    # Unicode emoji only (👀) — forward verbatim, translation would mangle them
    if not _HAS_WORD_RE.search(clean):
        plan.segments.append(_SegmentPlan(text, None, {}, [], []))
        return plan

    plan.custom_emojis = emojis

    # Split by newlines and translate each line independently when special
    # handling is needed for part of the message.
    # Use split("\n") rather than splitlines() — splitlines() also breaks on
    # \v, \f, \x1c-\x1e, \x85, U+2028/U+2029, none of which Discord renders
    # as a line break, so treating them as one would fragment the message.
    lines = clean.split("\n")

    def _line_needs_extraction(line: str) -> bool:
        """True if this line mixes translatable words with a Unicode emoji
        or URL, which need to be pulled out before translation. A blank
        line or one that's entirely emoji/URL (no other words once those
        are removed) passes through untouched either way, so it doesn't
        force per-line handling — mirrors the per-line loop's own segment
        check below.
        """
        stripped = line.strip()
        if not stripped:
            return False
        remainder = _URL_RE.sub("", _UNICODE_EMOJI_RE.sub("", stripped)).strip()
        if not remainder or not _HAS_WORD_RE.search(remainder):
            return False
        return bool(_UNICODE_EMOJI_RE.search(stripped) or _URL_RE.search(stripped))

    def _line_matches_glossary(line: str) -> bool:
        """True if this line actually contains a glossary term that would
        produce a placeholder for this dest language — mirrors _apply_glossary's
        own matching logic. A guild simply *having* a glossary configured
        (e.g. proper nouns like "Jim") must not disable the fast path for
        every message on that guild; only a real match on this line should.
        """
        if not glossary:
            return False
        for term, translations in glossary.items():
            if dest not in translations and "*" not in translations:
                continue
            if _term_pattern(term).search(line):
                return True
        return False

    # Fast path: a message manually broken across multiple lines (a common
    # casual chat style, e.g. one word/phrase per line for emphasis) loses
    # all sentence context when each line is translated independently —
    # e.g. "打破" alone becomes "break in" instead of "break". If no line
    # needs special handling (a real glossary match, or a Unicode emoji/URL
    # mixed in with real words), translate the whole block in one call so the
    # provider keeps cross-line context. A trailing emoji-only line (very
    # common) doesn't block this — it is preserved unchanged either way.
    if (
        len(lines) > 1
        and not any(_line_needs_extraction(line) or _line_matches_glossary(line) for line in lines)
    ):
        plan.segments.append(_SegmentPlan(clean, clean, {}, [], []))
        return plan

    for line in lines:
        line_stripped = line.strip()
        if not line_stripped or not _HAS_WORD_RE.search(line_stripped):
            plan.segments.append(_SegmentPlan(line, None, {}, [], []))
            continue

        # Strip Unicode emojis and URLs — both confuse/stall the translation API
        line_emojis = _UNICODE_EMOJI_RE.findall(line_stripped)
        line_urls = _URL_RE.findall(line_stripped)
        segment = _URL_RE.sub("", _UNICODE_EMOJI_RE.sub("", line_stripped)).strip()
        if not segment or not _HAS_WORD_RE.search(segment):
            plan.segments.append(_SegmentPlan(line_stripped, None, {}, [], []))
            continue

        placeholder_map: dict[str, str] = {}
        if glossary:
            segment, placeholder_map = _apply_glossary(segment, dest, glossary)

        # Full glossary matches need no provider. Partial matches retain the
        # raw placeholders for grouping/cache and restore per target later.
        provider_text = segment
        original = line_stripped
        if placeholder_map and not re.sub(r"§\d+§", "", segment).strip():
            provider_text = None
            restored = _restore_glossary(segment, placeholder_map)
            if restored:
                original = restored
            else:
                # Empty glossary values historically fall back to the line,
                # with its URL/emoji placement preserved.
                line_urls = []
                line_emojis = []
        plan.segments.append(
            _SegmentPlan(original, provider_text, placeholder_map, line_urls, line_emojis)
        )

    return plan


def _render_target_plan(
    plan: _TargetPlan, segment_results: dict[int, str | None]
) -> TranslationOutcome:
    if plan.terminal_none:
        return TranslationOutcome(None, True)

    succeeded = True
    rendered = []
    for index, segment in enumerate(plan.segments):
        if segment.provider_text is None:
            body = segment.original
        else:
            body = segment_results.get(index)
            if not body:
                succeeded = False
                if not segment.placeholder_map:
                    # Failed plain segments preserve the original placement
                    # of URLs and emoji, just like the scalar pipeline.
                    rendered.append(segment.original)
                    continue
                body = segment.provider_text
            body = _restore_glossary(body, segment.placeholder_map)
            if not body:
                rendered.append(segment.original)
                continue
        if segment.urls:
            body += "  " + " ".join(segment.urls)
        if segment.unicode_emojis:
            body += "  " + " ".join(segment.unicode_emojis)
        rendered.append(body)

    result = plan.separator.join(rendered)
    if result and plan.custom_emojis:
        result += "  " + " ".join(plan.custom_emojis)
    if plan.mentions:
        result = "  ".join(part for part in (result, " ".join(plan.mentions)) if part)
    return TranslationOutcome(result or None, succeeded)


def _translate_many_with_source_status(
    text: str,
    source_lang: str,
    target_langs: list[str],
    glossary: dict | None = None,
    substitutions: dict | None = None,
    _use_cache: bool = True,
) -> dict[str, TranslationOutcome]:
    """Plan each target, share identical provider inputs, then render separately."""
    source = normalize_lang(source_lang)
    provider_source = _provider_source_language(text, source)
    targets = list(dict.fromkeys(normalize_lang(target) for target in target_langs))
    plans = {
        target: _build_target_plan(text, source, target, glossary, substitutions)
        for target in targets
    }
    results: dict[str, dict[int, str | None]] = {target: {} for target in targets}
    jobs: dict[str, list[tuple[str, int]]] = {}
    cache = None
    for target, plan in plans.items():
        for index, segment in enumerate(plan.segments):
            if segment.provider_text is None:
                continue
            if _use_cache and cache is None:
                cache = _get_translate_cache()
            key = (segment.provider_text, provider_source, target)
            cached = cache.get(key) if cache is not None else None
            if cached:
                results[target][index] = cached
            else:
                jobs.setdefault(segment.provider_text, []).append((target, index))

    for provider_text, consumers in jobs.items():
        job_targets = list(dict.fromkeys(target for target, _ in consumers))
        translated: dict[str, str | None] = {
            target: provider_text
            for target in job_targets
            if target == provider_source
        }
        provider_targets = [
            target for target in job_targets if target != provider_source
        ]
        if len(targets) == 1 and provider_targets:
            # Preserve the scalar hook used by existing integrations; the
            # adapter delegates to the provider's batch API when available.
            target = provider_targets[0]
            translated[target] = _translate_with_fallback(
                provider_text, provider_source, target
            )
        elif provider_targets:
            provider_results = _get_provider_chain().translate_many(
                provider_text, provider_source, provider_targets
            )
            translated.update(provider_results)
            for target in provider_targets:
                _log_translate_event(
                    provider_source, target, provider_text, translated.get(target)
                )
        for target in job_targets:
            value = translated.get(target)
            if value and cache is not None:
                cache[(provider_text, provider_source, target)] = value
            if not value:
                log_event(
                    f"[translate] all attempts failed "
                    f"({provider_source}->{target}): {repr(provider_text)}"
                )
        for target, index in consumers:
            results[target][index] = translated.get(target)

    return {target: _render_target_plan(plan, results[target]) for target, plan in plans.items()}


def translate_many_with_status(
    text: str,
    target_langs: list[str],
    glossary: dict | None = None,
    substitutions: dict | None = None,
    _use_cache: bool = True,
) -> dict[str, TranslationOutcome]:
    """Translate unique canonical targets using provider source detection."""
    return _translate_many_with_source_status(
        text, "auto", target_langs, glossary, substitutions, _use_cache
    )


def translate_text_with_status(
    text: str,
    source_lang: str,
    target_lang: str,
    glossary: dict | None = None,
    substitutions: dict | None = None,
    _use_cache: bool = True,
) -> TranslationOutcome:
    """Translate while preserving whether every required provider call succeeded."""
    target = normalize_lang(target_lang)
    return _translate_many_with_source_status(
        text, source_lang, [target], glossary, substitutions, _use_cache
    )[target]


def translate_text(
    text: str,
    source_lang: str,
    target_lang: str,
    glossary: dict | None = None,
    substitutions: dict | None = None,
    _use_cache: bool = True,
) -> str | None:
    return translate_text_with_status(
        text, source_lang, target_lang, glossary, substitutions, _use_cache
    ).text


def get_translation_status(*, probe_libre: bool = True) -> dict:
    """Return the provider's sanitized snapshot and optional bounded probe."""
    chain = _get_provider_chain()
    result = chain.status_snapshot()
    result["libretranslate_probe"] = (
        chain.probe_libretranslate(timeout=5.0) if probe_libre else None
    )
    return result


def translate_text_nocache(
    text: str,
    source_lang: str,
    target_lang: str,
    glossary: dict | None = None,
    substitutions: dict | None = None,
) -> str | None:
    """Same as translate_text but bypasses the LRU cache — use for re-translation feedback."""
    return translate_text(text, source_lang, target_lang, glossary, substitutions, _use_cache=False)
