import re
import time
from functools import lru_cache
from deep_translator import GoogleTranslator

# Build a lowercase-keyed lookup so user input like "zh-tw" maps to "zh-TW"
_SUPPORTED: dict[str, str] = {
    v.lower(): v
    for v in GoogleTranslator().get_supported_languages(as_dict=True).values()
}

# Discord custom emoji: <:name:id> or animated <a:name:id>
_CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")
# Matches any real word character (letters/digits from any script, incl. CJK)
_HAS_WORD_RE = re.compile(r"\w", re.UNICODE)


def normalize_lang(code: str) -> str:
    return _SUPPORTED.get(code.lower(), code)


def _try_google(text: str, source: str, target: str, retries: int = 4) -> str | None:
    """Call Google Translate with retries for both rate-limit errors and untranslated results."""
    for attempt in range(retries):
        try:
            result = GoogleTranslator(source=source, target=target).translate(text)
            if result and result.strip() != text.strip():
                return result
            # Result equals input — Google returned original text unchanged.
            # Wait and retry; this is usually a temporary API issue.
            if attempt < retries - 1:
                wait = 2 ** attempt  # 1 s, 2 s, 4 s
                print(f"[translate] result==input ({source}->{target}) attempt {attempt+1}, retrying in {wait}s")
                time.sleep(wait)
        except Exception as e:
            err = str(e).lower()
            if any(k in err for k in ("429", "too many", "rate limit", "quota")):
                if attempt < retries - 1:
                    wait = 2 ** attempt
                    print(f"[translate] rate limit ({source}->{target}) attempt {attempt+1}, retrying in {wait}s")
                    time.sleep(wait)
                continue
            print(f"Google Translate error (source={source}, target={target}): {e}")
            break
    return None


@lru_cache(maxsize=2000)
def _cached_translate(text: str, src: str, dest: str) -> str | None:
    print(f"[translate] ({src}->{dest}) input: {repr(text)}")
    # Try explicit source first — more reliable for CJK languages
    result = _try_google(text, src, dest)
    if not result:
        # Auto-detect as secondary attempt
        result = _try_google(text, "auto", dest)
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
                _try_google(segment, src, dest)
                or _try_google(segment, "auto", dest)
            )
        else:
            line_result = _cached_translate(segment, src, dest)

        if not line_result:
            print(f"[translate] all attempts failed ({src}->{dest}): {repr(line_stripped)}")
            translated_lines.append(line_stripped)
            continue

        if placeholder_map:
            line_result = _restore_glossary(line_result, placeholder_map)

        translated_lines.append(line_result)

    result = "\n".join(translated_lines)
    if not result:
        return None

    if emojis:
        result = result + "  " + " ".join(emojis)

    return result
