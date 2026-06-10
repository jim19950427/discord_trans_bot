from translator import _apply_glossary, _restore_glossary


def test_glossary_case_insensitive():
    glossary = {"JIM": {"en": "James"}}
    text, placeholders = _apply_glossary("hi jim!", "en", glossary)
    result = _restore_glossary(text, placeholders)
    assert result == "hi James!"


def test_glossary_word_boundary_not_matched():
    glossary = {"JIM": {"en": "James"}}
    text, placeholders = _apply_glossary("JIMMY says hi", "en", glossary)
    result = _restore_glossary(text, placeholders)
    assert result == "JIMMY says hi"


def test_glossary_cjk_substring():
    glossary = {"小明": {"en": "Xiao Ming"}}
    text, placeholders = _apply_glossary("小明說小明很乖", "en", glossary)
    result = _restore_glossary(text, placeholders)
    assert result == "Xiao Ming說Xiao Ming很乖"


def test_glossary_mixed_term():
    glossary = {"Jim老師": {"en": "Teacher Jim"}}
    text, placeholders = _apply_glossary("大家好，Jim老師在嗎？", "en", glossary)
    result = _restore_glossary(text, placeholders)
    assert result == "大家好，Teacher Jim在嗎？"
