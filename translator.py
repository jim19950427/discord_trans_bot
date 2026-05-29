from deep_translator import GoogleTranslator

# Build a lowercase-keyed lookup so user input like "zh-tw" maps to "zh-TW"
_SUPPORTED: dict[str, str] = {
    v.lower(): v
    for v in GoogleTranslator().get_supported_languages(as_dict=True).values()
}


def normalize_lang(code: str) -> str:
    """Return the correctly-cased language code expected by deep-translator."""
    return _SUPPORTED.get(code.lower(), code)


def translate_text(text: str, source_lang: str, target_lang: str) -> str | None:
    src = normalize_lang(source_lang)
    dest = normalize_lang(target_lang)
    if src == dest:
        return None
    try:
        result = GoogleTranslator(source=src, target=dest).translate(text)
        return result
    except Exception as e:
        print(f"Translation error ({src} -> {dest}): {e}")
        return None
