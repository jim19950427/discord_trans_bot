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
# 套用前先把現有檔案備份成 <檔名>.bak；任何一個檔案寫入失敗（磁碟滿、
# 權限）就用備份把已動到的檔案全部還原，不會留下新舊混合的程式。
# 結束碼：2 = 備份失敗（尚未動任何檔案）、3 = 套用失敗（已還原並驗證）、
# 4 = 套用失敗且還原也失敗（保留所有 .bak，需手動處理）。
apply_all() {
    local rc=0
    ssh "${SSH_OPTS[@]}" "$NAS" sh -s -- "$DEST" "${FILES[@]}" <<'REMOTE' || rc=$?
dest="$1"; shift
fail=0
for f in "$@"; do
    if [ -f "$dest/$f" ]; then
        cp -p "$dest/$f" "$dest/$f.bak" || fail=1
    fi
done
if [ "$fail" = 1 ]; then
    for f in "$@"; do rm -f "$dest/$f.bak"; done
    exit 2
fi
for f in "$@"; do
    if ! cat "$dest/$f.new" > "$dest/$f"; then fail=1; break; fi
done
if [ "$fail" = 1 ]; then
    same() { [ "$(cksum < "$1")" = "$(cksum < "$2")" ]; }
    restore_fail=0
    for f in "$@"; do
        [ -f "$dest/$f.bak" ] || continue
        # Skip files the failed run never touched (e.g. one that rejected the
        # write); only a verified restore counts.
        same "$dest/$f.bak" "$dest/$f" && continue
        if ! { cat "$dest/$f.bak" > "$dest/$f" && same "$dest/$f.bak" "$dest/$f"; }; then
            restore_fail=1
        fi
    done
    # Keep every .bak if any restore failed: they are the only good copy.
    if [ "$restore_fail" = 1 ]; then exit 4; fi
    for f in "$@"; do rm -f "$dest/$f.bak"; done
    exit 3
fi
for f in "$@"; do rm -f "$dest/$f.new" "$dest/$f.bak"; done
REMOTE
    case "$rc" in
        0) ;;
        2) error "NAS 上備份現有檔案失敗（磁碟空間？），尚未套用任何檔案；.new 暫存檔仍在 NAS 上" ;;
        3) error "套用中途失敗，已用備份還原所有檔案（NAS 仍是舊版）；.new 暫存檔仍在 NAS 上" ;;
        4) error "套用失敗，而且還原也失敗！NAS 上的 *.bak 是唯一完好的舊版，已全部保留；請先手動還原（cat 檔名.bak > 檔名）再重啟容器" ;;
        *) error "套用時 SSH 失敗（結束碼 ${rc}）；請檢查 NAS 上的 .bak / .new 檔案是否需要手動處理" ;;
    esac
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
    sleep "${DEPLOY_POLL_INTERVAL:-5}"
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
