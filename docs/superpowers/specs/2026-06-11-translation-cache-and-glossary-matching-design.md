# 持久化翻譯快取 + 詞彙表比對改進 設計文件

日期：2026-06-11

## 背景

`translator.py` 目前有兩個可以改進的地方：

1. `_cached_translate` 使用 `functools.lru_cache(maxsize=2000)`，快取只存在於程式記憶體中，容器重啟後全部消失，常見句子需要重新呼叫 Google Translate（容易撞到 429 限流）。
2. `_apply_glossary` 使用 `term not in text` / `text.replace(term, ph)` 做子字串比對，對英文/拼音類詞彙會有「JIM 誤匹配 JIMMY」的問題，且完全區分大小寫。

本設計涵蓋這兩項改進，以及對應的單元測試。

## 一、持久化翻譯快取

### 架構

- 移除 `_cached_translate` 上的 `@lru_cache(maxsize=2000)` decorator，改用 `diskcache.Cache` 實例。
- 快取目錄：`/data/translate_cache`，可透過環境變數 `TRANSLATE_CACHE_DIR` 覆寫（與 `glossary.py` 中其他 `/data` 路徑風格一致）。
- 容量上限：環境變數 `TRANSLATE_CACHE_SIZE_LIMIT`（bytes），預設約 50MB（`50 * 1024 * 1024`）。
- Eviction 策略：`eviction_policy="least-recently-used"`，行為上接近原本 `lru_cache` 的 LRU 語意。
- Cache key：維持 `(text, src, dest)` tuple，與現行邏輯一致。

### 行為變更：失敗結果不快取

目前 `lru_cache` 連 `_translate_with_fallback` 回傳 `None`（翻譯失敗）的結果也會快取住，導致同一句話在程式存活期間永遠回傳 `None`，即使 Google Translate 之後恢復正常。

新實作中：

- 翻譯成功（非 `None`）→ 寫入 diskcache，下次直接命中。
- 翻譯失敗（`None`）→ **不寫入** diskcache，下次呼叫會重新嘗試。

### 依賴變更

`requirements.txt` 新增：

```
diskcache>=5.6.0
```

## 二、詞彙表比對改進

### 位置

`translator.py` 的 `_apply_glossary`。

### 比對策略

依詞彙內容（`term.isascii()`）分兩種比對方式：

1. **純 ASCII 詞彙**（例如 `JIM`、`Boss`）：
   - 使用 word-boundary regex：`(?<![A-Za-z0-9])` + `re.escape(term)` + `(?![A-Za-z0-9])`，並加上 `re.IGNORECASE`。
   - 不分大小寫比對（`jim` / `Jim` / `JIM` 皆命中），命中後一律替換成詞彙表設定的翻譯/保留字（不保留原始大小寫）。
   - 避免「JIM」誤匹配「JIMMY」內的子字串。

2. **含非 ASCII 字元的詞彙**（CJK 或中英混合，例如「小明」「Jim老師」）：
   - 維持原本的子字串比對 `term in text` / `text.replace(term, ph)`，因為 CJK 沒有空白分詞，`\b` 無法正確判斷詞界。

### 不變的部分

- placeholder 機制（`§N§`）與 `_restore_glossary` 還原邏輯不變。
- 「整行都被詞彙表覆蓋則跳過翻譯，直接還原」的邏輯不變。
- `translations["*"]`（保留原文）與一般翻譯目標語言的選取邏輯不變。

## 三、測試

目前專案沒有測試框架。新增：

- `requirements-dev.txt`：內容為 `pytest`（疊加在 `requirements.txt` 之上，僅供開發/CI 使用）。
- `tests/test_glossary_matching.py`
- `tests/test_translate_cache.py`

### 詞彙表測試案例

| 測試案例 | 輸入 | 斷言 |
|---|---|---|
| 大小寫不敏感 | 詞彙表 `JIM`→`{"en": "James"}`；訊息 `"hi jim!"` | 結果為 `"hi James!"` |
| 詞界判斷 | 詞彙表 `JIM`→`{"en": "James"}`；訊息 `"JIMMY says hi"` | 結果中 `"JIMMY"` 保持不變 |
| CJK 子字串（既有行為不變） | 詞彙表 `小明`→`{"en": "Xiao Ming"}`；訊息 `"小明說小明很乖"` | 兩處 `小明` 都被替換 |
| 中英混合詞彙 | 詞彙表 `Jim老師`→`{"en": "Teacher Jim"}`；訊息含 `"Jim老師"` | 該詞被替換（走 CJK 子字串分支） |

### 翻譯快取測試案例

| 測試案例 | 做法 | 斷言 |
|---|---|---|
| 重啟後仍有快取 | 用 `tmp_path` 開一個 `Cache`，寫入一筆翻譯結果後關閉；再開一個新的 `Cache` 指向同一路徑，呼叫 `_cached_translate`（mock 底層翻譯函式） | 1) 回傳值與寫入值相同 2) mock 的翻譯函式 `call_count == 0` |
| 失敗結果不快取 | mock `_translate_with_fallback` 回傳 `None`，連續呼叫 `_cached_translate` 兩次（相同參數） | mock 的 `call_count == 2`（第二次仍重新嘗試） |

執行方式：

```bash
pip install -r requirements-dev.txt
pytest
```

測試使用 `tmp_path` 暫存目錄與 mock，不依賴 Discord、不會呼叫真實的 Google Translate API、不會動到正式 `/data` 資料。

## 範圍外（Out of scope）

- 翻譯引擎 fallback（仍只用 Google Translate via deep-translator）。
- 管理後台（export/import slash command 為未來獨立項目）。
