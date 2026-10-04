#!/bin/bash
# deploy.sh — 把程式碼上傳到 DSM（Synology NAS）的 docker 資料夾。
#
# NAS 不支援 scp/rsync，改用 SSH pipe（cat 檔案 | ssh 'cat > 目標'）。
# 上傳後容器內的 file watcher 偵測到 mtime 變更會自動重啟，不需手動動 NAS。
#
# 用法：
#   ./deploy.sh              只上傳程式碼（bot.py 等）— 日常改碼用
#   ./deploy.sh --with-deps  連 docker-compose.yml / Dockerfile / requirements.txt
#                            一起上傳（首次部署或改依賴時用；改依賴後仍需在
#                            Container Manager 重建 image）
set -e

# ── 設定 ──────────────────────────────────────────────
# 可用環境變數覆寫，例如走 Tailscale：DEPLOY_NAS=jim@my-nas ./deploy.sh
NAS="${DEPLOY_NAS:-jim@192.168.1.11}"
DEST="${DEPLOY_DEST:-/volume1/docker/discord-trans-bot}"
DIR="$(cd "$(dirname "$0")" && pwd)"

CODE_FILES=(bot.py translator.py translation_providers.py config.py glossary.py)
DEP_FILES=(docker-compose.yml Dockerfile requirements.txt)

# ── 顏色 ──────────────────────────────────────────────
RED='\033[91m'; GREEN='\033[92m'; CYAN='\033[96m'; YELLOW='\033[93m'; BOLD='\033[1m'; RESET='\033[0m'
success() { echo -e "${GREEN}${BOLD}[ OK ]${RESET}  $*"; }
info()    { echo -e "${CYAN}${BOLD}[INFO]${RESET}  $*"; }
warn()    { echo -e "${YELLOW}${BOLD}[WARN]${RESET}  $*"; }
error()   { echo -e "${RED}${BOLD}[ERR ]${RESET}  $*"; exit 1; }

SSH_OPTS=(-o ConnectTimeout=8 -o BatchMode=yes)

# 階段 1：上傳到 <檔名>.new 並驗證位元組數。
# 傳到一半斷線只會留下 .new，不會動到正在跑的程式（watcher 不監看 .new）。
stage() {
    local f="$1" size remote_size
    [ -f "${DIR}/${f}" ] || error "找不到檔案：${f}"
    size=$(wc -c < "${DIR}/${f}" | tr -d ' ')
    cat "${DIR}/${f}" | ssh "${SSH_OPTS[@]}" "$NAS" "cat > '${DEST}/${f}.new'" \
        || error "上傳失敗：${f}"
    remote_size=$(ssh "${SSH_OPTS[@]}" "$NAS" "wc -c < '${DEST}/${f}.new'" | tr -d ' ')
    [ "$size" = "$remote_size" ] || error "${f} 大小不符（本機 ${size}，NAS ${remote_size}），未套用"
    success "${f}  已上傳並驗證（${size} bytes）"
}

# 階段 2：在 NAS 上用 cat 原地覆蓋（NAS 本機複製，毫秒級）。
# 不能用 mv：docker-compose 是單檔 bind mount，mv 會換 inode，
# 容器會一直看到舊檔，熱重載就失效。所有檔案在同一個 ssh 內一次套用，
# 避免 watcher 在多檔上傳之間重啟而載入新舊混合的版本。
apply_all() {
    local cmd="set -e;" f
    for f in "${FILES[@]}"; do
        cmd+=" cat '${DEST}/${f}.new' > '${DEST}/${f}' && rm -f '${DEST}/${f}.new';"
    done
    ssh "${SSH_OPTS[@]}" "$NAS" "$cmd" || error "套用失敗（.new 暫存檔仍在 NAS 上）"
}

echo -e "\n${BOLD}${CYAN}🚀  Discord Trans Bot — 部署到 DSM${RESET}\n"

# 決定要上傳哪些檔案
FILES=("${CODE_FILES[@]}")
WITH_DEPS=0
if [ "$1" == "--with-deps" ]; then
    WITH_DEPS=1
    FILES=("${CODE_FILES[@]}" "${DEP_FILES[@]}")
    info "模式：程式碼 + 部署設定（--with-deps）"
else
    info "模式：只上傳程式碼（${CODE_FILES[*]}）"
fi

# 連線測試
info "測試 SSH 連線 ${NAS} ..."
ssh "${SSH_OPTS[@]}" "$NAS" "test -d '${DEST}'" \
    || error "無法連線或找不到目錄 ${DEST}（確認 SSH key 與路徑）"
success "連線正常，目標目錄存在"

STATUS_FILE="${DEST}/data/status.json"
read_status() { ssh "${SSH_OPTS[@]}" "$NAS" "cat '${STATUS_FILE}' 2>/dev/null" || true; }
BEFORE="$(read_status)"

echo ""
for f in "${FILES[@]}"; do
    stage "$f"
done
apply_all
success "已套用 ${#FILES[@]} 個檔案"
info  "等待容器 file watcher 偵測變更並重啟（最多 60 秒）..."

# 部署後驗證：status.json 的 last_start 變了，才代表真的重啟
RESTARTED=0
for _ in $(seq 1 12); do
    sleep 5
    AFTER="$(read_status)"
    if [ -n "$AFTER" ] && [ "$AFTER" != "$BEFORE" ]; then
        RESTARTED=1
        break
    fi
done
if [ "$RESTARTED" -eq 1 ]; then
    success "容器已重啟：${AFTER}"
else
    warn "60 秒內沒看到 status.json 更新。請到 Container Manager 查看容器狀態與日誌。"
fi

if [ "$WITH_DEPS" -eq 1 ]; then
    echo ""
    warn "你上傳了 requirements.txt / Dockerfile。"
    warn "依賴或 image 有變動時，需到 DSM Container Manager 重建 image 才會生效。"
fi
echo ""
