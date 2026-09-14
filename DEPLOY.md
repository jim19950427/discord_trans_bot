# 部署說明

> 📖 **指令與功能說明**請見 [README.md](README.md)

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
   - `管理訊息`（**必須**，用於同步置頂訊息、移除翻譯回報的 🔄 Reaction）
   - `建立公開討論串`（**必須**，用於在其他語言頻道建立對應討論串）
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

# 可選設定（有預設值，不填也可以）
# MAX_CLUSTER_ENTRIES=2000            ← 追蹤訊息上限（預設 2000，超過時自動淘汰最舊的）
# TRANSLATE_CACHE_DIR=/data/translate_cache   ← 翻譯快取目錄（預設值如左，已包含在 data volume 內，重啟不會消失）
# TRANSLATE_CACHE_SIZE_LIMIT=52428800         ← 翻譯快取容量上限，單位 bytes（預設 50MB，超過時自動淘汰最少使用的項目）
# BOT_LOG_FILE=/data/bot_log.json      ← 機器人運作紀錄檔（JSON），涵蓋翻譯呼叫與所有錯誤/事件訊息，方便除錯查詢
# BOT_LOG_MAX_ENTRIES=5000             ← 紀錄檔上限筆數（預設 5000，超過時自動淘汰最舊的紀錄）
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

## 四、Azure 與 NAS LibreTranslate 備援

1. 只在 NAS 的 `/volume1/docker/discord-trans-bot/.env` 設定 Azure Key 1：`AZURE_TRANSLATOR_KEY=...`；`AZURE_TRANSLATOR_REGION=eastasia`，endpoint 使用 `https://api.cognitive.microsofttranslator.com`。不要把真實 key 貼進終端機參數、shell history 或聊天。
2. 上傳新版後執行 `./deploy.sh --with-deps`，再到 Container Manager 重新建置 `discord-trans-bot` image，因為 Python 依賴已變更。
3. 啟動 LibreTranslate。第一次下載模型可能需要數分鐘，health status 顯示 `starting` 是正常的；其 port 不會發布到 NAS 外部。
4. 以 SSH 驗證：

```bash
cd /volume1/docker/discord-trans-bot
sudo docker compose ps
sudo docker compose exec discord-trans-bot python -c "import requests; print([x['code'] for x in requests.get('http://libretranslate:5000/languages', timeout=10).json()])"
```

5. 用 Discord 傳送測試訊息驗證 Azure，並檢查已清除敏感資料的 provider event：

```bash
python3 -c 'import json; p="/volume1/docker/discord-trans-bot/data/bot_log.json"; rows=json.load(open(p)); print(*[({k:r.get(k) for k in ("time","provider","success","latency_ms","fallback_reason","circuit_state")}) for r in rows if r.get("type")=="translate_provider"][-10:], sep="\n")'
```

6. 要測試備援時，暫時把 NAS `.env` 的 key 改為字面值 `invalid-test-key`，只重建 bot container，送一段未快取文字，確認 `provider=libretranslate`；隨即還原 Key 1 並再次重建 bot。切勿將真 key 放在命令列。
7. 輪替金鑰時先讓 bot 改用 Key 2、重建並確認 Azure 成功，最後才在 Azure portal 重新產生 Key 1。
