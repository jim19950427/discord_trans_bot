import os
import re
import json
import time
import threading
import diskcache
import deep_translator.google as _deep_google
from deep_translator import GoogleTranslator

# ── Google Translate scraper hardening ────────────────────────────────────
# deep-translator calls requests.get() without a User-Agent, so requests sends
# "python-requests/x.y". Google began blocking that UA in Aug 2026: it answers
# HTTP 200 (so deep-translator's status-code check passes) but puts its own
# "Error 500 (Server Error)" page in the result slot instead of a translation.
# A browser UA gets normal results from the very same endpoint.
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


class _UARequestsProxy:
    """Wraps the requests module so every GET carries a browser User-Agent.

    deep-translator exposes no hook for request headers, so the only place to
    inject one is the `requests` reference inside its google module.
    """

    def __init__(self, real):
        self._real = real

    def get(self, *args, **kwargs):
        headers = dict(kwargs.pop("headers", None) or {})
        headers.setdefault("User-Agent", _BROWSER_UA)
        return self._real.get(*args, headers=headers, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


if not isinstance(_deep_google.requests, _UARequestsProxy):
    _deep_google.requests = _UARequestsProxy(_deep_google.requests)

CACHE_DIR = os.getenv("TRANSLATE_CACHE_DIR", "/data/translate_cache")
CACHE_SIZE_LIMIT = int(os.getenv("TRANSLATE_CACHE_SIZE_LIMIT", str(50 * 1024 * 1024)))

LOG_FILE = os.getenv("BOT_LOG_FILE", "/data/bot_log.json")
LOG_MAX_ENTRIES = int(os.getenv("BOT_LOG_MAX_ENTRIES", "5000"))
_log_lock = threading.Lock()

_translate_cache: diskcache.Cache | None = None


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

# Build a lowercase-keyed lookup so user input like "zh-tw" maps to "zh-TW"
_SUPPORTED: dict[str, str] = {
    v.lower(): v
    for v in GoogleTranslator().get_supported_languages(as_dict=True).values()
}

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
# URLs — Google Translate returns them unchanged, causing pointless retries
_URL_RE = re.compile(r"https?://\S+")
# Matches any real word character (letters/digits from any script, incl. CJK)
_HAS_WORD_RE = re.compile(r"\w", re.UNICODE)
# Characters str.splitlines() treats as line boundaries but that Discord
# never renders as visible line breaks — typically invisible copy-paste
# artifacts (e.g. \x1d Group Separator) from other apps. Left in place,
# they fragment a message into spurious per-word "lines" during translation.
_STRAY_LINEBREAK_RE = re.compile("[\v\f\x1c\x1d\x1e\x85\u2028\u2029]")
# Google's /m endpoint answers HTTP 200 even when it fails, embedding its own
# error page in the result slot — so deep-translator returns that page's text
# as if it were a translation. It doesn't equal the input, so the plain
# result!=input check accepts it, and _cached_translate then stores the garbage
# permanently. These markers are Google's error-page signature: a "Error NNN
# (Server Error)" banner and the "That's all we know." footer. Both are far too
# distinctive to collide with real chat text, and _looks_like_error_page still
# refuses to fire when the source text already contained the marker itself.
_ERROR_PAGE_RE = re.compile(
    r"error \d{3} \(server error\)|that['\u2019]s all we know",
    re.IGNORECASE,
)


def _looks_like_error_page(result: str, source_text: str) -> bool:
    """True if `result` is Google's error page rather than a translation."""
    if not _ERROR_PAGE_RE.search(result):
        return False
    # Don't misfire on a message that genuinely contains the phrase.
    return not _ERROR_PAGE_RE.search(source_text)



def normalize_lang(code: str) -> str:
    return _SUPPORTED.get(code.lower(), code)


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


def log_event(message: str, **fields) -> None:
    """Print message to the console (unchanged, still visible in the DSM log
    viewer) and also append a structured entry to a shared JSON log file,
    capped at LOG_MAX_ENTRIES entries (oldest dropped first). Thread-safe —
    bot events and translate calls both come from concurrent worker threads.
    """
    print(message)
    entry = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "type": fields.pop("type", "info"),
        "message": message,
        **fields,
    }
    with _log_lock:
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                entries = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            entries = []
        entries.append(entry)
        if len(entries) > LOG_MAX_ENTRIES:
            entries = entries[-LOG_MAX_ENTRIES:]
        try:
            os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                json.dump(entries, f, ensure_ascii=False)
        except OSError as e:
            print(f"[log write failed] {e}")


def _log_translate_event(src: str, dest: str, text: str, result: str | None) -> None:
    message = f"[translate] ({src}->{dest}) {repr(text)} -> {repr(result)}"
    log_event(message, type="translate", src=src, dest=dest, input=text, output=result)


def _try_google(text: str, source: str, target: str, retries: int = 4) -> str | None:
    """Call Google Translate with retries for rate limits and no-result
    errors (exponential backoff, up to `retries` attempts).

    A result that equals the input isn't a transient failure — it means
    this content just has no different translation (timestamps, leftover
    glossary placeholders, decoratively-spaced text, etc.), so retrying
    with the full exponential backoff only wastes time. That case gets its
    own much smaller retry budget instead.
    """
    SAME_INPUT_RETRIES = 2
    SAME_INPUT_WAIT = 1
    # An error page means Google itself is failing, not that we were throttled.
    # When it happens it tends to be persistent (a blocked client, a broken
    # endpoint), so probing briefly and then degrading to untranslated text
    # beats stalling every language of every message on the full backoff.
    ERROR_PAGE_RETRIES = 2
    ERROR_PAGE_WAIT = 1
    same_input_attempts = 0
    error_page_attempts = 0
    for attempt in range(retries):
        try:
            result = GoogleTranslator(source=source, target=target).translate(text)
            if result and _looks_like_error_page(result, text):
                # Never return or cache this — it would be posted to Discord as
                # if it were the translation.
                error_page_attempts += 1
                log_event(
                    f"[translate] error page from Google ({source}->{target}) "
                    f"attempt {error_page_attempts}: {result[:80]!r}"
                )
                if error_page_attempts < ERROR_PAGE_RETRIES:
                    time.sleep(ERROR_PAGE_WAIT)
                    continue
                break
            if result and result.strip() != text.strip():
                return result
            # Result equals input — Google returned original text unchanged.
            same_input_attempts += 1
            if same_input_attempts < SAME_INPUT_RETRIES:
                log_event(f"[translate] result==input ({source}->{target}) attempt {same_input_attempts}, retrying in {SAME_INPUT_WAIT}s")
                time.sleep(SAME_INPUT_WAIT)
            else:
                break
        except Exception as e:
            err = str(e).lower()
            retryable = any(k in err for k in (
                "429", "too many", "rate limit", "quota", "no translation was found"
            ))
            if retryable:
                if attempt < retries - 1:
                    wait = 2 ** attempt
                    log_event(f"[translate] retryable error ({source}->{target}) attempt {attempt+1}: {e}, retrying in {wait}s")
                    time.sleep(wait)
                continue
            log_event(f"Google Translate error (source={source}, target={target}): {e}")
            break
    return None


def _source_variants(src: str) -> list[str]:
    """Return source codes to try in order. CJK sources get extra fallbacks.

    Note: bare "zh" is deliberately not included — deep_translator's
    GoogleTranslator only supports "zh-CN"/"zh-TW", and "zh" always raises
    "No support for the provided language", wasting an API call every time.
    """
    lower = src.lower()
    if lower == "zh-tw":
        return [src, "zh-CN", "auto"]
    if lower == "zh-cn":
        return [src, "auto"]
    return [src, "auto"]


def _translate_with_fallback(text: str, src: str, dest: str) -> str | None:
    """Try source variants in order until one returns a translation."""
    for source in _source_variants(src):
        result = _try_google(text, source, dest)
        if result:
            _log_translate_event(src, dest, text, result)
            return result
    _log_translate_event(src, dest, text, None)
    return None


def _cached_translate(text: str, src: str, dest: str, cache: diskcache.Cache | None = None) -> str | None:
    if cache is None:
        cache = _get_translate_cache()

    key = (text, src, dest)
    if key in cache:
        return cache[key]

    result = _translate_with_fallback(text, src, dest)

    if result is not None:
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


def translate_text(
    text: str,
    source_lang: str,
    target_lang: str,
    glossary: dict | None = None,
    substitutions: dict | None = None,
    _use_cache: bool = True,
) -> str | None:
    src = normalize_lang(source_lang)
    dest = normalize_lang(target_lang)
    if src == dest:
        return None

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

    def _with_mentions(body: str) -> str:
        if not mentions:
            return body
        parts = [p for p in (body, " ".join(mentions)) if p]
        return "  ".join(parts)

    # Apply pre-translation substitutions (e.g. aliases / euphemisms)
    if substitutions:
        for src_term, replacement in substitutions.items():
            text = re.sub(re.escape(src_term), replacement, text, flags=re.IGNORECASE)

    # Pull out custom Discord emojis — Google Translate chokes on <:name:id> syntax
    emojis = _CUSTOM_EMOJI_RE.findall(text)
    clean = _CUSTOM_EMOJI_RE.sub("", text).strip()

    if not clean:
        return _with_mentions(text) if (emojis or mentions) else None

    # Unicode emoji only (👀) — forward verbatim, translation would mangle them
    if not _HAS_WORD_RE.search(clean):
        return _with_mentions(text)

    # Split by newlines and translate each line independently to avoid a
    # deep-translator / unofficial Google API bug where only the first line
    # gets translated when the input contains newlines.
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
    # mixed in with real words), translate the whole block in one call so
    # Google keeps cross-line context. A trailing emoji-only line (very
    # common) doesn't block this — Google passes it through unchanged either way.
    if (
        len(lines) > 1
        and not any(_line_needs_extraction(line) or _line_matches_glossary(line) for line in lines)
    ):
        block_result = (_cached_translate if _use_cache else _translate_with_fallback)(clean, src, dest)
        if not block_result:
            log_event(f"[translate] all attempts failed ({src}->{dest}): {repr(clean)}")
            block_result = clean
        if emojis:
            block_result = block_result + "  " + " ".join(emojis)
        return _with_mentions(block_result)

    translated_lines: list[str] = []
    for line in lines:
        line_stripped = line.strip()
        if not line_stripped or not _HAS_WORD_RE.search(line_stripped):
            translated_lines.append(line)
            continue

        # Strip Unicode emojis and URLs — both confuse/stall the translation API
        line_emojis = _UNICODE_EMOJI_RE.findall(line_stripped)
        line_urls = _URL_RE.findall(line_stripped)
        segment = _URL_RE.sub("", _UNICODE_EMOJI_RE.sub("", line_stripped)).strip()
        if not segment or not _HAS_WORD_RE.search(segment):
            translated_lines.append(line_stripped)
            continue

        placeholder_map: dict[str, str] = {}
        if glossary:
            segment, placeholder_map = _apply_glossary(segment, dest, glossary)

        if placeholder_map:
            # If the entire line is covered by glossary placeholders, skip
            # translation and restore directly — glossary takes priority.
            remainder = re.sub(r"§\d+§", "", segment).strip()
            if not remainder:
                line_result = _restore_glossary(segment, placeholder_map)
            else:
                line_result = _translate_with_fallback(segment, src, dest)
                if line_result:
                    line_result = _restore_glossary(line_result, placeholder_map)
                else:
                    line_result = _restore_glossary(segment, placeholder_map)
        else:
            line_result = (_cached_translate if _use_cache else _translate_with_fallback)(segment, src, dest)

        if not line_result:
            log_event(f"[translate] all attempts failed ({src}->{dest}): {repr(segment)}")
            translated_lines.append(line_stripped)
            continue

        if line_urls:
            line_result = line_result + "  " + " ".join(line_urls)
        if line_emojis:
            line_result = line_result + "  " + " ".join(line_emojis)

        translated_lines.append(line_result)

    result = "\n".join(translated_lines)
    if not result:
        return _with_mentions("") if mentions else None

    if emojis:
        result = result + "  " + " ".join(emojis)

    return _with_mentions(result)


def translate_text_nocache(
    text: str,
    source_lang: str,
    target_lang: str,
    glossary: dict | None = None,
    substitutions: dict | None = None,
) -> str | None:
    """Same as translate_text but bypasses the LRU cache — use for re-translation feedback."""
    return translate_text(text, source_lang, target_lang, glossary, substitutions, _use_cache=False)
