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
NAS="jim@192.168.1.11"
DEST="/volume1/docker/discord-trans-bot"
DIR="$(cd "$(dirname "$0")" && pwd)"

CODE_FILES=(bot.py translator.py config.py glossary.py)
DEP_FILES=(docker-compose.yml Dockerfile requirements.txt)

# ── 顏色 ──────────────────────────────────────────────
RED='\033[91m'; GREEN='\033[92m'; CYAN='\033[96m'; YELLOW='\033[93m'; BOLD='\033[1m'; RESET='\033[0m'
success() { echo -e "${GREEN}${BOLD}[ OK ]${RESET}  $*"; }
info()    { echo -e "${CYAN}${BOLD}[INFO]${RESET}  $*"; }
warn()    { echo -e "${YELLOW}${BOLD}[WARN]${RESET}  $*"; }
error()   { echo -e "${RED}${BOLD}[ERR ]${RESET}  $*"; exit 1; }

# 上傳單一檔案（SSH pipe）
upload() {
    local f="$1"
    [ -f "${DIR}/${f}" ] || error "找不到檔案：${f}"
    if cat "${DIR}/${f}" | ssh "$NAS" "cat > '${DEST}/${f}'"; then
        success "${f}  →  ${NAS}:${DEST}/${f}"
    else
        error "上傳失敗：${f}"
    fi
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
    info "模式：只上傳程式碼（bot.py translator.py config.py glossary.py）"
fi

# 連線測試
info "測試 SSH 連線 ${NAS} ..."
ssh -o ConnectTimeout=8 "$NAS" "test -d '${DEST}'" \
    || error "無法連線或找不到目錄 ${DEST}（確認 SSH key 與路徑）"
success "連線正常，目標目錄存在"

echo ""
for f in "${FILES[@]}"; do
    upload "$f"
done

echo ""
success "上傳完成"
info  "容器 file watcher 會在 ~10 秒內偵測變更並自動重啟"

if [ "$WITH_DEPS" -eq 1 ]; then
    echo ""
    warn "你上傳了 requirements.txt / Dockerfile。"
    warn "依賴或 image 有變動時，需到 DSM Container Manager 重建 image 才會生效。"
fi
echo ""
