# Discord 多語言翻譯機器人

自動橋接多個語言頻道。任何人在語言頻道發訊息，機器人會以**該用戶的名稱與頭像**，將翻譯後的內容同步發送到其他所有語言頻道。

**[➕ 點此將機器人加入你的伺服器](https://discord.com/oauth2/authorize?client_id=1509841382242648154&permissions=536963136&integration_type=0&scope=bot+applications.commands)**

## 運作方式

```
A 在 #中文 輸入「你好」
    ↓
機器人以 A 的名字與頭像發送：
    #english  → Hello
    #日本語    → こんにちは
    #한국어    → 안녕하세요

B 在 #english 輸入「Good morning」
    ↓
機器人以 B 的名字與頭像發送：
    #中文     → 早安
    #日本語    → おはようございます
    #한국어    → 좋은 아침이에요
```

---

## 一、建立 Discord Bot

### 1. 建立應用程式

1. 前往 [Discord Developer Portal](https://discord.com/developers/applications)
2. 右上角點擊 **New Application**
3. 輸入機器人名稱（例如 `翻譯機器人`），點擊 **Create**

### 2. 建立 Bot 並取得 Token

1. 左側選單點擊 **Bot**
2. 點擊 **Add Bot** → **Yes, do it!**
3. 在 **TOKEN** 區塊點擊 **Reset Token**，複製產生的 Token（**只會顯示一次，請妥善保存**）
4. 往下找到 **Privileged Gateway Intents**，開啟以下兩個選項：
   - **Message Content Intent** ✅（必須開啟，否則機器人讀不到訊息內容）
   - **Server Members Intent** ✅（用於取得用戶顯示名稱）
5. 點擊 **Save Changes**

### 3. 設定 OAuth2 邀請連結（Bot 權限）

1. 左側選單點擊 **OAuth2** → **URL Generator**
2. **Scopes** 勾選：
   - `bot`
   - `applications.commands`（**必須**，用於 Slash 指令）
3. **Bot Permissions** 勾選：
   - `傳送訊息`
   - `讀取訊息歷史記錄`
   - `管理 Webhook`（**必須**，機器人會自動在頻道建立 Webhook）
   - `新增反應`（**必須**，用於同步各頻道的 Reaction）
   - `管理訊息`（**必須**，用於同步置頂訊息）
   - `嵌入連結`
4. 複製頁面下方產生的 URL，在瀏覽器開啟，選擇你的伺服器並授權

> **關於 Webhook**：你不需要手動建立 Webhook。當你執行 `/setlang` 指令時，機器人會自動在該頻道建立一個名為 `TranslationBot` 的 Webhook，並將 URL 儲存在設定檔中。這個 Webhook 讓機器人能以原始用戶的名字和頭像發送翻譯後的訊息。

---

## 二、部署到 Synology DSM（Container Manager 專案模式）

### 1. 上傳專案檔案

1. 開啟 **File Station**
2. 進入 `docker` 資料夾（若無則新建）
3. 建立子資料夾，例如 `discord_trans_bot`
4. 將以下所有檔案上傳到該資料夾：

```
discord_trans_bot/
├── bot.py
├── translator.py
├── config.py
├── glossary.py
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env              ← 你需要自己建立此檔案（見下方說明）
└── data/             ← 建立此空資料夾（儲存頻道設定與詞彙表）
```

### 2. 建立 `.env` 檔案

在 File Station 中，於 `discord_trans_bot` 資料夾內建立一個純文字檔，命名為 `.env`，內容如下：

```
DISCORD_TOKEN=你的Bot_Token貼在這裡
```

> 若 File Station 不允許建立以點開頭的檔案，可先命名為 `env.txt` 上傳後再改名，或透過 SSH 建立。

### 3. 建立 `data` 資料夾

在 `discord_trans_bot` 資料夾內建立一個名為 `data` 的空資料夾，用來持久化頻道設定與詞彙表。

### 4. 使用 Container Manager 建立專案

1. 開啟 **Container Manager**
2. 左側選單點擊 **專案（Project）**
3. 點擊右上角 **新增（Create）**
4. 填寫專案資訊：
   - **專案名稱**：`discord-trans-bot`（自訂）
   - **路徑**：選擇 `/docker/discord_trans_bot`
   - **來源**：選擇「**使用 docker-compose.yml 建立專案**」
5. 系統會自動讀取 `docker-compose.yml`，確認內容後點擊 **下一步**
6. 點擊 **完成**，Container Manager 會自動建置 image 並啟動容器

### 5. 確認運行狀態

- 在 Container Manager → **專案** 中，看到狀態顯示為 **執行中（Running）** 即表示成功
- 點擊容器名稱 → **日誌（Log）**，應看到類似以下輸出：
  ```
  Logged in as 翻譯機器人#1234 (ID: 123456789)
  Loaded channel configs for 0 guild(s)
  Synced 6 slash command(s)
  ```

---

## 三、更新程式（不需重新建置 Image）

由於原始碼透過 Volume 掛載，更新流程非常簡單：

1. 透過 **File Station** 將新版的 `.py` 檔案上傳覆蓋至 `/docker/discord_trans_bot/`
2. 開啟 **Container Manager** → **專案**
3. 點擊 `discord-trans-bot` 專案 → **停止（Stop）** → **啟動（Start）**
4. 完成，新程式立即生效

> **什麼時候才需要重新建置 Image？**  
> 只有當 `requirements.txt` 內的套件版本有變更時，才需要在 Container Manager 專案中選擇 **重新建置（Build）**。一般程式邏輯的更新不需要此步驟。

---

## 四、Discord 指令設定

機器人啟動後，在你的 Discord 伺服器中執行以下指令進行初始設定。  
**Slash 指令**（`/`）為主要方式，舊版 `!` 前綴指令仍可使用。

> **注意**：Slash 指令在機器人首次啟動後最多需要 **1 小時**才會在 Discord 全域生效。等待期間可先使用 `!` 前綴版本（例如 `!setlang zh-TW`）。

### 初始設定範例

```
/setlang lang_code:zh-TW channel:#中文
/setlang lang_code:en channel:#english
/setlang lang_code:ja channel:#日本語
/setlang lang_code:ko channel:#한국어
```

執行後機器人會自動在各頻道建立 Webhook，之後所有訊息會自動翻譯並同步。

### 頻道管理指令

| Slash 指令 | 前綴指令（備用） | 所需權限 | 說明 |
|-----------|----------------|---------|------|
| `/setlang lang_code:<代碼> [channel:#頻道]` | `!setlang <代碼> [#頻道]` | 管理頻道 | 將頻道設為語言頻道（省略頻道則為目前頻道） |
| `/unsetlang [channel:#頻道]` | `!unsetlang [#頻道]` | 管理頻道 | 取消語言頻道設定並刪除對應 Webhook |
| `/listlang` | `!listlang` | 所有人 | 列出目前所有語言頻道 |

### 詞彙表指令

詞彙表可固定特定詞彙的翻譯，避免伺服器名稱、遊戲術語等被錯誤翻譯。

**設定步驟：**

1. 決定要固定的來源詞彙（例如你的伺服器名稱 `星辰公會`）
2. 為每個目標語言分別新增一筆條目：
   ```
   /addterm word:星辰公會 lang:en translation:Starlight Guild
   /addterm word:星辰公會 lang:ja translation:スターライトギルド
   /addterm word:星辰公會 lang:ko translation:별빛 길드
   ```
3. 之後有人傳送含有「星辰公會」的訊息時，其他語言頻道會固定使用你設定的翻譯，而不是依賴機器翻譯。

**運作原理：** 翻譯前，機器人先將訊息中的來源詞彙替換成特殊佔位符，翻譯完成後再以你設定的目標語言翻譯結果取代，確保術語不會被亂翻。

| Slash 指令 | 所需權限 | 說明 |
|-----------|---------|------|
| `/addterm word:<詞彙> lang:<代碼> translation:<翻譯>` | 管理頻道 | 新增詞彙表條目 |
| `/removeterm word:<詞彙> [lang:<代碼>]` | 管理頻道 | 移除詞彙表條目（省略 lang 則刪除該詞彙所有語言） |
| `/listterms` | 所有人 | 列出目前所有詞彙表條目 |

---

## 五、功能說明

### 訊息同步

| 功能 | 說明 |
|------|------|
| 翻譯轉發 | 以原始用戶名稱與頭像，透過 Webhook 發送翻譯後的訊息 |
| 附件轉發 | 圖片、檔案同步轉發到所有語言頻道 |
| 貼圖轉發 | PNG／GIF 貼圖以圖片附件形式轉發（Lottie 向量格式除外） |
| 回覆引用 | 回覆訊息時，其他頻道以 blockquote 顯示被引用的翻譯內容與發送者姓名，例如：`> **夏希下井**: 它很害怕。` |
| Embed 轉發 | 連結預覽（Link Preview）會在 Discord 產生後自動同步到所有語言頻道 |

### 訊息操作同步

| 功能 | 說明 |
|------|------|
| 編輯同步 | 編輯訊息時，其他頻道的翻譯也同步更新 |
| 附件編輯同步 | 編輯時若附件有變動，刪除舊訊息並重新發送 |
| 刪除同步 | 刪除訊息時，其他頻道的翻譯訊息一併刪除 |
| 批量刪除同步 | 一次批量刪除多則訊息也會全部同步 |
| Reaction 同步 | 新增、移除、清除 Reaction 均同步到所有對應訊息 |
| 置頂同步 | 置頂或取消置頂訊息時，其他頻道同步操作（需要「管理訊息」權限） |

### 翻譯引擎

| 功能 | 說明 |
|------|------|
| 自動偵測語言 | 優先使用 `source=auto` 自動偵測，對混合語言訊息更準確 |
| 翻譯快取 | 相同內容不重複呼叫 API，節省資源 |
| 限流重試 | 遇到 Google Translate 限流（429）時自動退避重試（1 秒、2 秒） |
| 備用翻譯引擎 | Google Translate 全部重試失敗後自動切換至 MyMemory |

### 特殊行為

| 功能 | 說明 |
|------|------|
| 不翻譯前綴 `//` | 訊息以 `//` 開頭時，不翻譯、不轉發，只在來源頻道顯示。例如輸入 `// 這段不要翻譯` |
| 純 Emoji 訊息 | 只有 Emoji 的訊息（如 👀）直接原文轉發，不嘗試翻譯（避免 Google 亂翻） |
| Discord 自訂表情 | `<:name:id>` 格式的自訂表情從翻譯中抽出，接在翻譯結果後方 |

---

## 六、已知限制

| 限制 | 說明 |
|------|------|
| 容器重啟後追蹤記憶體清空 | 編輯、刪除、Reaction、置頂同步只對**容器重啟後**發送的訊息有效；重啟前的舊訊息無法追蹤 |
| Reaction 以 Bot 帳號顯示 | Discord API 不允許 Bot 代替其他用戶新增 Reaction，因此同步的 Reaction 會顯示為機器人帳號所新增 |
| Lottie 貼圖無法轉發 | Discord 向量動畫貼圖（Lottie 格式）為 JSON 檔案，無法轉成圖片，會直接跳過 |
| Slash 指令最多等待 1 小時 | Discord 全域 Slash 指令在首次啟動後最多需要 1 小時才會對所有伺服器生效 |
| 詞彙表不區分來源語言 | 詞彙表的來源詞彙是直接比對訊息原文，不會依頻道語言篩選 |

---

## 七、語言代碼參考

| 代碼 | 語言 | 中文說明 |
|------|------|---------|
| `zh-TW` | 繁體中文 | 繁體中文 |
| `zh-CN` | 簡體中文 | 簡體中文 |
| `en` | English | 英文 |
| `ja` | 日本語 | 日文 |
| `ko` | 한국어 | 韓文 |
| `fr` | Français | 法文 |
| `de` | Deutsch | 德文 |
| `es` | Español | 西班牙文 |
| `vi` | Tiếng Việt | 越南文 |
| `th` | ภาษาไทย | 泰文 |
| `id` | Bahasa Indonesia | 印尼文 |
| `ru` | Русский | 俄文 |
| `ar` | العربية | 阿拉伯文 |
| `pl` | Polski | 波蘭文 |

完整語言代碼清單請參考：https://py-googletrans.readthedocs.io/en/latest/
