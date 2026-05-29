import re
import time
from functools import lru_cache
from deep_translator import GoogleTranslator, MyMemoryTranslator

# Build a lowercase-keyed lookup so user input like "zh-tw" maps to "zh-TW"
_SUPPORTED: dict[str, str] = {
    v.lower(): v
    for v in GoogleTranslator().get_supported_languages(as_dict=True).values()
}

# Discord custom emoji: <:name:id> or animated <a:name:id>
_CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")
# Matches any real word character (letters/digits from any script, incl. CJK)
_HAS_WORD_RE = re.compile(r"\w", re.UNICODE)

# MyMemory uses locale codes; map common lang codes to them
_MYMEMORY_MAP: dict[str, str] = {
    "af": "af-ZA", "ar": "ar-SA", "bg": "bg-BG", "cs": "cs-CZ",
    "da": "da-DK", "de": "de-DE", "el": "el-GR", "en": "en-US",
    "es": "es-ES", "et": "et-EE", "fi": "fi-FI", "fr": "fr-FR",
    "he": "he-IL", "hi": "hi-IN", "hr": "hr-HR", "hu": "hu-HU",
    "id": "id-ID", "it": "it-IT", "ja": "ja-JP", "ko": "ko-KR",
    "lt": "lt-LT", "lv": "lv-LV", "ms": "ms-MY", "nl": "nl-NL",
    "no": "no-NO", "pl": "pl-PL", "pt": "pt-PT", "ro": "ro-RO",
    "ru": "ru-RU", "sk": "sk-SK", "sl": "sl-SI", "sr": "sr-RS",
    "sv": "sv-SE", "th": "th-TH", "tr": "tr-TR", "uk": "uk-UA",
    "vi": "vi-VN", "zh-CN": "zh-CN", "zh-TW": "zh-TW",
}


def normalize_lang(code: str) -> str:
    return _SUPPORTED.get(code.lower(), code)


def _try_google(text: str, source: str, target: str) -> str | None:
    """Call Google Translate with up to 3 retries on rate-limit errors."""
    for attempt in range(3):
        try:
            result = GoogleTranslator(source=source, target=target).translate(text)
            if result:
                return result
        except Exception as e:
            err = str(e).lower()
            if any(k in err for k in ("429", "too many", "rate limit", "quota")):
                if attempt < 2:
                    time.sleep(2 ** attempt)  # 1 s, 2 s
                continue
            print(f"Google Translate error (source={source}, target={target}): {e}")
            break
    return None


def _try_mymemory(text: str, src: str, dest: str) -> str | None:
    """MyMemory fallback for languages present in _MYMEMORY_MAP."""
    mm_src = _MYMEMORY_MAP.get(src)
    mm_dest = _MYMEMORY_MAP.get(dest)
    if not mm_src or not mm_dest:
        return None
    try:
        result = MyMemoryTranslator(source=mm_src, target=mm_dest).translate(text)
        return result or None
    except Exception as e:
        print(f"MyMemory fallback error ({src} -> {dest}): {e}")
        return None


@lru_cache(maxsize=2000)
def _cached_translate(text: str, src: str, dest: str) -> str | None:
    """Translate with LRU cache. Tries auto-detect first, then declared source, then MyMemory."""
    print(f"[translate] ({src}->{dest}) input: {repr(text)}")
    result = _try_google(text, "auto", dest)
    if not result:
        result = _try_google(text, src, dest)
    if not result:
        result = _try_mymemory(text, src, dest)
        if result:
            print(f"MyMemory fallback used ({src} -> {dest})")
    print(f"[translate] ({src}->{dest}) output: {repr(result)}")
    return result


def _apply_glossary(text: str, dest: str, glossary: dict) -> tuple[str, dict[str, str]]:
    """Replace source terms with §N§ placeholders so they survive translation."""
    placeholder_map: dict[str, str] = {}
    for idx, (term, translations) in enumerate(glossary.items()):
        if term in text and dest in translations:
            ph = f"§{idx}§"
            text = text.replace(term, ph)
            placeholder_map[ph] = translations[dest]
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
) -> str | None:
    src = normalize_lang(source_lang)
    dest = normalize_lang(target_lang)
    if src == dest:
        return None

    # Pull out custom Discord emojis — Google Translate chokes on <:name:id> syntax
    emojis = _CUSTOM_EMOJI_RE.findall(text)
    clean = _CUSTOM_EMOJI_RE.sub("", text).strip()

    if not clean:
        return text if emojis else None

    # Unicode emoji only (👀) — forward verbatim, translation would mangle them
    if not _HAS_WORD_RE.search(clean):
        return text

    # Split by newlines and translate each line independently to avoid a
    # deep-translator / unofficial Google API bug where only the first line
    # gets translated when the input contains newlines.
    lines = clean.splitlines()
    translated_lines: list[str] = []
    for line in lines:
        line_stripped = line.strip()
        if not line_stripped or not _HAS_WORD_RE.search(line_stripped):
            translated_lines.append(line)
            continue

        placeholder_map: dict[str, str] = {}
        segment = line_stripped
        if glossary:
            segment, placeholder_map = _apply_glossary(segment, dest, glossary)

        if placeholder_map:
            line_result = (
                _try_google(segment, "auto", dest)
                or _try_google(segment, src, dest)
                or _try_mymemory(segment, src, dest)
            )
        else:
            line_result = _cached_translate(segment, src, dest)

        if not line_result:
            return None

        if placeholder_map:
            line_result = _restore_glossary(line_result, placeholder_map)

        translated_lines.append(line_result)

    result = "\n".join(translated_lines)
    if not result:
        return None

    if emojis:
        result = result + "  " + " ".join(emojis)

    return result
