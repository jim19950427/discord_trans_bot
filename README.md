# Discord 多語言翻譯機器人

自動橋接多個語言頻道。任何人在語言頻道發訊息，機器人會以**該用戶的名稱與頭像**，將翻譯後的內容同步發送到其他所有語言頻道。

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
3. **Bot Permissions** 勾選：
   - `傳送訊息`
   - `讀取訊息歷史記錄`
   - `管理 Webhook`（**必須**，機器人會自動在頻道建立 Webhook）
   - `新增反應`（**必須**，用於同步各頻道的 Reaction）
   - `嵌入連結`
4. 複製頁面下方產生的 URL，在瀏覽器開啟，選擇你的伺服器並授權

> **關於 Webhook**：你不需要手動建立 Webhook。當你在 Discord 執行 `!setlang` 指令時，機器人會自動在該頻道建立一個名為 `TranslationBot` 的 Webhook，並將 URL 儲存在設定檔中。這個 Webhook 讓機器人能以原始用戶的名字和頭像發送翻譯後的訊息。

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
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env              ← 你需要自己建立此檔案（見下方說明）
└── data/             ← 建立此空資料夾（儲存頻道設定）
```

### 2. 建立 `.env` 檔案

在 File Station 中，於 `discord_trans_bot` 資料夾內建立一個純文字檔，命名為 `.env`，內容如下：

```
DISCORD_TOKEN=你的Bot_Token貼在這裡
```

> 若 File Station 不允許建立以點開頭的檔案，可先命名為 `env.txt` 上傳後再改名，或透過 SSH 建立。

### 3. 建立 `data` 資料夾

在 `discord_trans_bot` 資料夾內建立一個名為 `data` 的空資料夾，用來持久化頻道設定。

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

機器人啟動後，在你的 Discord 伺服器中執行以下指令進行初始設定（需要「管理頻道」權限）：

```
!setlang zh-TW #中文
!setlang en #english
!setlang ja #日本語
!setlang ko #한국어
```

執行後機器人會自動在各頻道建立 Webhook，之後所有訊息會自動翻譯並同步。

### 所有指令

| 指令 | 所需權限 | 說明 |
|------|---------|------|
| `!setlang <語言代碼> [#頻道]` | 管理頻道 | 將頻道設為語言頻道（省略 #頻道 則為目前頻道） |
| `!unsetlang [#頻道]` | 管理頻道 | 取消語言頻道設定並刪除對應 Webhook |
| `!listlang` | 所有人 | 列出目前所有語言頻道 |

---

## 五、語言代碼參考

| 代碼 | 語言 |
|------|------|
| `zh-TW` | 繁體中文 |
| `zh-CN` | 簡體中文 |
| `en` | English |
| `ja` | 日本語 |
| `ko` | 한국어 |
| `fr` | Français |
| `de` | Deutsch |
| `es` | Español |
| `vi` | Tiếng Việt |
| `th` | ภาษาไทย |

完整語言代碼清單請參考：https://py-googletrans.readthedocs.io/en/latest/
