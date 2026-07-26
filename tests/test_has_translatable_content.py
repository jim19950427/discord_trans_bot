from translator import has_translatable_content


def test_custom_emoji_only_has_no_translatable_content():
    assert has_translatable_content("<:WOS:1489240870485233757>") is False


def test_mention_only_has_no_translatable_content():
    assert has_translatable_content("<@1234567890>") is False


def test_unicode_emoji_only_has_no_translatable_content():
    assert has_translatable_content("🤯🤯🤯🤯") is False


def test_mention_plus_custom_emoji_has_no_translatable_content():
    assert has_translatable_content("<@1234567890> <:WOS:1489240870485233757>") is False


def test_plain_text_has_translatable_content():
    assert has_translatable_content("帥氣的Jim") is True


def test_mention_plus_text_has_translatable_content():
    assert has_translatable_content("<@1234567890> hello") is True


def test_empty_string_has_no_translatable_content():
    assert has_translatable_content("") is False
