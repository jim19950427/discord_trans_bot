# Discord 多語言翻譯機器人

自動橋接多個語言頻道。任何人在語言頻道發訊息，機器人會以**該用戶的名稱與頭像**，將翻譯後的內容同步發送到其他所有語言頻道。

**[➕ 點此將機器人加入你的伺服器](https://discord.com/oauth2/authorize?client_id=1509841382242648154&permissions=536963136&integration_type=0&scope=bot+applications.commands)**

> 📖 **指令與功能說明**請見 [USAGE.md](USAGE.md)

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

> **關於 Webhook**：你不需要手動建立 Webhook。當你執行 `/addlang` 指令時，機器人會自動在該頻道建立一個名為 `TranslationBot` 的 Webhook，並將 URL 儲存在設定檔中。這個 Webhook 讓機器人能以原始用戶的名字和頭像發送翻譯後的訊息。

> **⚠️ 關於置頂同步（重要）**：Discord 的頻道層級權限可能會覆蓋角色設定。若置頂同步無效，請確認 Bot 在**每個語言頻道**都有「**管理訊息**」權限：前往伺服器設定 → 頻道 → 編輯各語言頻道 → 權限 → 找到 Bot 角色 → 開啟「管理訊息」✅。或直接在**伺服器設定 → 角色**中給 Bot 角色全伺服器的「管理訊息」權限（更方便）。

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
