# Translation Cache & Glossary Matching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the in-memory `lru_cache` translation cache with a persistent `diskcache`-backed cache, and improve glossary term matching to be case-insensitive with word boundaries for ASCII terms while preserving substring matching for CJK terms.

**Architecture:** Both changes live entirely in `translator.py`, which contains pure functions with no Discord/network dependencies (network calls are mockable). Add a `tests/` directory with `pytest` covering the new glossary matching rules and cache persistence/failure behavior. A root-level `conftest.py` makes `translator.py` importable from `tests/`.

**Tech Stack:** Python 3.11, `diskcache` (new dependency), `pytest` (new dev-only dependency), `unittest.mock`.

---

### Task 1: Project setup — add dependencies

**Files:**
- Modify: `requirements.txt`
- Create: `requirements-dev.txt`

- [ ] **Step 1: Add `diskcache` to `requirements.txt`**

Current content of `requirements.txt`:
```
discord.py>=2.3.2
deep-translator>=1.11.4
python-dotenv>=1.0.0
aiohttp>=3.9.0
```

New content:
```
discord.py>=2.3.2
deep-translator>=1.11.4
python-dotenv>=1.0.0
aiohttp>=3.9.0
diskcache>=5.6.0
```

- [ ] **Step 2: Create `requirements-dev.txt`**

```
-r requirements.txt
pytest>=8.0.0
```

- [ ] **Step 3: Install dependencies**

Run: `pip install -r requirements-dev.txt`
Expected: `diskcache` and `pytest` (and their dependencies) install successfully with no errors.

- [ ] **Step 4: Commit**

```bash
git add requirements.txt requirements-dev.txt
git commit -m "Add diskcache and pytest dependencies"
```

---

### Task 2: Glossary matching — case-insensitive word boundaries for ASCII terms

**Files:**
- Create: `conftest.py` (project root)
- Create: `tests/test_glossary_matching.py`
- Modify: `translator.py:87-105` (`_apply_glossary`)

- [ ] **Step 1: Create root `conftest.py` so `tests/` can import `translator`**

```python
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
```

- [ ] **Step 2: Write failing tests in `tests/test_glossary_matching.py`**

```python
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
```

- [ ] **Step 3: Run tests to verify expected failures**

Run: `pytest tests/test_glossary_matching.py -v`
Expected:
- `test_glossary_case_insensitive` — FAILED (`assert "hi jim!" == "hi James!"`)
- `test_glossary_word_boundary_not_matched` — FAILED (`assert "JamesMY says hi" == "JIMMY says hi"`)
- `test_glossary_cjk_substring` — PASSED (existing substring matching already handles this)
- `test_glossary_mixed_term` — PASSED (existing substring matching already handles this)

- [ ] **Step 4: Implement word-boundary + case-insensitive matching for ASCII terms**

Replace `translator.py:87-105` (the current `_apply_glossary` function) with:

```python
def _term_pattern(term: str) -> re.Pattern:
    """Word-boundary, case-insensitive pattern for ASCII glossary terms."""
    return re.compile(
        r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])",
        re.IGNORECASE,
    )


def _apply_glossary(text: str, dest: str, glossary: dict) -> tuple[str, dict[str, str]]:
    """Replace source terms with §N§ placeholders so they survive translation.

    translations["*"] = original term means "keep as-is in all languages" (proper noun).

    ASCII terms (e.g. "JIM") are matched case-insensitively with word
    boundaries so "JIM" doesn't match inside "JIMMY". Terms containing
    non-ASCII characters (CJK or mixed) are matched as plain substrings,
    since CJK text has no whitespace word boundaries.
    """
    placeholder_map: dict[str, str] = {}
    for idx, (term, translations) in enumerate(glossary.items()):
        is_ascii = term.isascii()
        pattern = _term_pattern(term) if is_ascii else None

        if is_ascii:
            if not pattern.search(text):
                continue
        else:
            if term not in text:
                continue

        if dest in translations:
            replacement = translations[dest]
        elif "*" in translations:
            replacement = translations["*"]
        else:
            continue

        ph = f"§{idx}§"
        if is_ascii:
            text = pattern.sub(ph, text)
        else:
            text = text.replace(term, ph)
        placeholder_map[ph] = replacement
    return text, placeholder_map
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_glossary_matching.py -v`
Expected: all 4 tests PASSED.

- [ ] **Step 6: Commit**

```bash
git add conftest.py tests/test_glossary_matching.py translator.py
git commit -m "Improve glossary matching: case-insensitive word boundaries for ASCII terms"
```

---

### Task 3: Persistent translation cache with diskcache

**Files:**
- Create: `tests/test_translate_cache.py`
- Modify: `translator.py:1-4` (imports)
- Modify: `translator.py:79-84` (`_cached_translate`)

- [ ] **Step 1: Write failing tests in `tests/test_translate_cache.py`**

```python
from unittest.mock import Mock

import diskcache

import translator


def test_cache_persists_across_restart(tmp_path, monkeypatch):
    cache_dir = str(tmp_path / "cache")
    mock_translate = Mock(return_value="你好")
    monkeypatch.setattr(translator, "_translate_with_fallback", mock_translate)

    cache1 = diskcache.Cache(cache_dir)
    result1 = translator._cached_translate("hello", "en", "zh-TW", cache=cache1)
    assert result1 == "你好"
    cache1.close()

    # Simulate a restart: open a fresh Cache instance over the same directory.
    cache2 = diskcache.Cache(cache_dir)
    result2 = translator._cached_translate("hello", "en", "zh-TW", cache=cache2)
    assert result2 == "你好"
    cache2.close()

    # The underlying translator should only have been called once.
    assert mock_translate.call_count == 1


def test_failed_translation_not_cached(tmp_path, monkeypatch):
    cache_dir = str(tmp_path / "cache")
    mock_translate = Mock(return_value=None)
    monkeypatch.setattr(translator, "_translate_with_fallback", mock_translate)

    cache = diskcache.Cache(cache_dir)
    result1 = translator._cached_translate("hello", "en", "zh-TW", cache=cache)
    result2 = translator._cached_translate("hello", "en", "zh-TW", cache=cache)
    cache.close()

    assert result1 is None
    assert result2 is None
    # Both calls should hit the translator since failures aren't cached.
    assert mock_translate.call_count == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_translate_cache.py -v`
Expected: both tests FAIL with `TypeError: _cached_translate() got an unexpected keyword argument 'cache'`.

- [ ] **Step 3: Update imports in `translator.py:1-4`**

Replace:
```python
import re
import time
from functools import lru_cache
from deep_translator import GoogleTranslator
```

With:
```python
import os
import re
import time
import diskcache
from deep_translator import GoogleTranslator
```

- [ ] **Step 4: Add cache configuration and lazy cache getter**

Insert immediately after the imports (before the `_SUPPORTED` block at what was line 6):

```python
CACHE_DIR = os.getenv("TRANSLATE_CACHE_DIR", "/data/translate_cache")
CACHE_SIZE_LIMIT = int(os.getenv("TRANSLATE_CACHE_SIZE_LIMIT", str(50 * 1024 * 1024)))

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
```

- [ ] **Step 5: Replace `_cached_translate` (`translator.py:79-84` in the original file)**

Replace:
```python
@lru_cache(maxsize=2000)
def _cached_translate(text: str, src: str, dest: str) -> str | None:
    print(f"[translate] ({src}->{dest}) input: {repr(text)}")
    result = _translate_with_fallback(text, src, dest)
    print(f"[translate] ({src}->{dest}) output: {repr(result)}")
    return result
```

With:
```python
def _cached_translate(text: str, src: str, dest: str, cache: diskcache.Cache | None = None) -> str | None:
    if cache is None:
        cache = _get_translate_cache()

    key = (text, src, dest)
    if key in cache:
        return cache[key]

    print(f"[translate] ({src}->{dest}) input: {repr(text)}")
    result = _translate_with_fallback(text, src, dest)
    print(f"[translate] ({src}->{dest}) output: {repr(result)}")

    if result is not None:
        cache[key] = result
    return result
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_translate_cache.py -v`
Expected: both tests PASSED.

- [ ] **Step 7: Run the full test suite**

Run: `pytest -v`
Expected: all 6 tests (4 from Task 2, 2 from Task 3) PASSED.

- [ ] **Step 8: Commit**

```bash
git add tests/test_translate_cache.py translator.py
git commit -m "Add persistent diskcache-backed translation cache"
```

---

## Out of scope (per design doc)

- Translation engine fallback (Google Translate via deep-translator only).
- Admin export/import slash commands.
