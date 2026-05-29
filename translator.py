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
    """Call Google Translate with retries for rate limits, untranslated results, and no-result errors."""
    for attempt in range(retries):
        try:
            result = GoogleTranslator(source=source, target=target).translate(text)
            if result and result.strip() != text.strip():
                return result
            # Result equals input — Google returned original text unchanged.
            if attempt < retries - 1:
                wait = 2 ** attempt
                print(f"[translate] result==input ({source}->{target}) attempt {attempt+1}, retrying in {wait}s")
                time.sleep(wait)
        except Exception as e:
            err = str(e).lower()
            retryable = any(k in err for k in (
                "429", "too many", "rate limit", "quota", "no translation was found"
            ))
            if retryable:
                if attempt < retries - 1:
                    wait = 2 ** attempt
                    print(f"[translate] retryable error ({source}->{target}) attempt {attempt+1}: {e}, retrying in {wait}s")
                    time.sleep(wait)
                continue
            print(f"Google Translate error (source={source}, target={target}): {e}")
            break
    return None


def _source_variants(src: str) -> list[str]:
    """Return source codes to try in order. CJK sources get extra fallbacks."""
    lower = src.lower()
    if lower == "zh-tw":
        return [src, "zh-CN", "zh", "auto"]
    if lower == "zh-cn":
        return [src, "zh", "auto"]
    return [src, "auto"]


def _translate_with_fallback(text: str, src: str, dest: str) -> str | None:
    """Try source variants in order until one returns a translation."""
    for source in _source_variants(src):
        result = _try_google(text, source, dest)
        if result:
            return result
    return None


@lru_cache(maxsize=2000)
def _cached_translate(text: str, src: str, dest: str) -> str | None:
    print(f"[translate] ({src}->{dest}) input: {repr(text)}")
    result = _translate_with_fallback(text, src, dest)
    print(f"[translate] ({src}->{dest}) output: {repr(result)}")
    return result


def _apply_glossary(text: str, dest: str, glossary: dict) -> tuple[str, dict[str, str]]:
    """Replace source terms with §N§ placeholders so they survive translation.

    translations["*"] = original term means "keep as-is in all languages" (proper noun).
    """
    placeholder_map: dict[str, str] = {}
    for idx, (term, translations) in enumerate(glossary.items()):
        if term not in text:
            continue
        if dest in translations:
            replacement = translations[dest]
        elif "*" in translations:
            replacement = translations["*"]
        else:
            continue
        ph = f"§{idx}§"
        text = text.replace(term, ph)
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
            line_result = _cached_translate(segment, src, dest)

        if not line_result:
            print(f"[translate] all attempts failed ({src}->{dest}): {repr(line_stripped)}")
            translated_lines.append(line_stripped)
            continue

        translated_lines.append(line_result)

    result = "\n".join(translated_lines)
    if not result:
        return None

    if emojis:
        result = result + "  " + " ".join(emojis)

    return result
