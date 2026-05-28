from deep_translator import GoogleTranslator


def translate_text(text: str, source_lang: str, target_lang: str) -> str | None:
    """Translate text from source_lang to target_lang using Google Translate."""
    if source_lang == target_lang:
        return None
    try:
        result = GoogleTranslator(source=source_lang, target=target_lang).translate(text)
        return result
    except Exception as e:
        print(f"Translation error ({source_lang} -> {target_lang}): {e}")
        return None
