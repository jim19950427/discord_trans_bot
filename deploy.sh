#!/usr/bin/env bash
# deploy.sh — upload source code to NAS via SSH pipe and trigger hot-reload
#
# Daily usage:   ./deploy.sh
# Dependency update: ./deploy.sh --with-deps
#
# First-time setup:
#   1. Fill in the settings block below.
#   2. Set up SSH key auth: ssh-copy-id NAS_USER@NAS_IP
#   3. chmod +x deploy.sh
#   4. Restart the container once manually (DSM Container Manager → restart)
#      so the hot-reload watcher takes effect for the first time.

# ── Settings ────────────────────────────────────────────────────────────────
NAS="nas_user@192.168.x.x"                       # ← SSH user@host for your NAS
DEST="/volume1/docker/discord_trans_bot"          # ← project folder on NAS
STATUS_FILE="$DEST/data/status.json"              # ← matches STATUS_FILE env var

# Files uploaded on every deploy
CODE_FILES=(bot.py translator.py config.py glossary.py)

# Files uploaded only with --with-deps
DEP_FILES=(docker-compose.yml Dockerfile requirements.txt)
# ────────────────────────────────────────────────────────────────────────────

set -e

# ── Helpers ─────────────────────────────────────────────────────────────────
upload() {
    local src="$1" dst="$2"
    echo "  uploading $src → $dst"
    cat "$src" | ssh "$NAS" "cat > '$dst'"
}

die() { echo "ERROR: $1" >&2; exit 1; }

# ── Pre-flight ───────────────────────────────────────────────────────────────
ssh "$NAS" "test -d '$DEST'" || die "DEST dir '$DEST' not found on NAS. Check NAS / DEST settings."

# ── Upload ───────────────────────────────────────────────────────────────────
echo "==> Uploading code files…"
for f in "${CODE_FILES[@]}"; do
    upload "$f" "$DEST/$f"
done

if [[ "$1" == "--with-deps" ]]; then
    echo "==> Uploading dependency files…"
    for f in "${DEP_FILES[@]}"; do
        upload "$f" "$DEST/$f"
    done
    echo ""
    echo "NOTE: dependency files updated. Rebuild the Docker image in DSM Container"
    echo "      Manager (or run: docker compose up -d --build) for changes to take effect."
fi

# ── Wait for restart ─────────────────────────────────────────────────────────
echo ""
echo "==> Waiting for container to restart (up to ~30 s)…"
OLD_START=$(ssh "$NAS" "cat '$STATUS_FILE' 2>/dev/null | python3 -c \"import sys,json; d=json.load(sys.stdin); print(d.get('last_start',''))\" 2>/dev/null" || true)

for i in $(seq 1 20); do
    sleep 3
    NEW_START=$(ssh "$NAS" "cat '$STATUS_FILE' 2>/dev/null | python3 -c \"import sys,json; d=json.load(sys.stdin); print(d.get('last_start',''))\" 2>/dev/null" || true)
    if [[ -n "$NEW_START" && "$NEW_START" != "$OLD_START" ]]; then
        echo "✓ Container restarted at $NEW_START"
        exit 0
    fi
done

echo "⚠️  Timed out waiting for restart. The watcher should still trigger within 10 s."
echo "   Check DSM Container Manager logs if the container hasn't restarted."
