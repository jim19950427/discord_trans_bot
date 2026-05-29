import re
from deep_translator import GoogleTranslator

# Build a lowercase-keyed lookup so user input like "zh-tw" maps to "zh-TW"
_SUPPORTED: dict[str, str] = {
    v.lower(): v
    for v in GoogleTranslator().get_supported_languages(as_dict=True).values()
}

# Discord custom emoji: <:name:id> or animated <a:name:id>
_CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")


def normalize_lang(code: str) -> str:
    return _SUPPORTED.get(code.lower(), code)


def translate_text(text: str, source_lang: str, target_lang: str) -> str | None:
    src = normalize_lang(source_lang)
    dest = normalize_lang(target_lang)
    if src == dest:
        return None

    # Pull out custom Discord emojis — Google Translate chokes on <:name:id> syntax
    emojis = _CUSTOM_EMOJI_RE.findall(text)
    clean = _CUSTOM_EMOJI_RE.sub("", text).strip()

    # Message is only custom emojis; forward verbatim, nothing to translate
    if not clean:
        return text if emojis else None

    # Try with the declared source first, then fall back to auto-detect
    # (handles mixed-language messages like "oi 這個不行？")
    result: str | None = None
    for source in (src, "auto"):
        try:
            result = GoogleTranslator(source=source, target=dest).translate(clean)
            if result:
                break
            print(f"Translation returned empty ({src} -> {dest}, source={source})")
        except Exception as e:
            print(f"Translation error ({src} -> {dest}, source={source}): {e}")

    if not result:
        return None

    # Re-attach emojis at the end
    if emojis:
        result = result + "  " + " ".join(emojis)

    return result
