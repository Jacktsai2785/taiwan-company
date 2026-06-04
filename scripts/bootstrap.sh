#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# scripts/bootstrap.sh — 一鍵安裝「台灣產業商情平台」的完整執行環境（冪等）。
#
# 在已 clone 的 repo 內執行：
#     bash scripts/bootstrap.sh
#
# 做完這些事：
#   1. 系統套件（git / tesseract+繁中 / ffmpeg）           ← 需 sudo
#   2. uv（Python 套件管理）
#   3. .venv + requirements.txt（含 playwright）
#   4. playwright Chromium + 系統共享庫                    ← install-deps 需 sudo
#   5. 偵測 / 安裝 claude CLI（本機 AI 引擎）
#   6. .env（從 .env.example 複製，若不存在）
#   7. data/ logs/ 目錄
#   8. 生成並啟動 systemd user service（開機自啟 + crash 重啟）
#   9. healthcheck（curl http://localhost:8003）
#
# 可重複執行：每一步都會偵測「已就緒就跳過」。
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

log()  { printf '\n\033[1;36m── %s ──\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m⚠ %s\033[0m\n' "$*"; }
err()  { printf '\033[1;31m✗ %s\033[0m\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

# 非 root 且有 sudo → 用 sudo；root → 直接跑；無 sudo → 標記，相關步驟改提示
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  if have sudo; then SUDO="sudo"; else warn "非 root 且無 sudo，需 root 權限的步驟會被跳過並改為提示"; fi
fi

# 確保 ~/.local/bin 在 PATH（uv / claude 常裝在這）
export PATH="$HOME/.local/bin:$PATH"

# ── 1. 系統套件 ───────────────────────────────────────────────────────────────
log "1/9 系統套件（tesseract 繁中 OCR、ffmpeg、git）"
SYS_PKGS=(git curl ca-certificates build-essential tesseract-ocr tesseract-ocr-chi-tra ffmpeg)
if have apt-get; then
  if [ -n "$SUDO" ] || [ "$(id -u)" -eq 0 ]; then
    $SUDO apt-get update -qq
    $SUDO apt-get install -y "${SYS_PKGS[@]}"
    ok "系統套件就緒"
  else
    warn "請以 sudo 手動安裝：apt-get install -y ${SYS_PKGS[*]}"
  fi
else
  warn "非 apt 系統，請以對應套件管理器安裝：${SYS_PKGS[*]}"
fi

# ── 2. uv ─────────────────────────────────────────────────────────────────────
log "2/9 uv（Python 套件管理）"
if have uv; then
  ok "uv 已安裝（$(uv --version)）"
else
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  have uv && ok "uv 安裝完成（$(uv --version)）" || { err "uv 安裝失敗"; exit 1; }
fi

# ── 3. .venv + Python 套件 ────────────────────────────────────────────────────
log "3/9 建立 .venv 並安裝 requirements.txt"
[ -d .venv ] || uv venv --python 3.11
uv pip install -r requirements.txt
ok "Python 套件就緒"

# ── 4. playwright Chromium + 系統庫 ───────────────────────────────────────────
log "4/9 playwright Chromium 瀏覽器與系統庫"
.venv/bin/python -m playwright install chromium
if [ -n "$SUDO" ] || [ "$(id -u)" -eq 0 ]; then
  # install-deps 由 playwright 官方處理跨 Ubuntu 版本的套件名（含 libnspr4 等）
  $SUDO .venv/bin/python -m playwright install-deps chromium \
    || warn "playwright install-deps 失敗（非 Debian/Ubuntu？）；findbiz 若報 libnspr4.so 缺失請手動補系統庫"
  ok "Chromium 與系統庫就緒"
else
  warn "無 sudo，略過系統庫安裝。findbiz 抓每股金額可能因缺 libnspr4.so 失敗。"
  warn "請手動執行：sudo .venv/bin/python -m playwright install-deps chromium"
fi

# ── 5. claude CLI（本機 AI 引擎）──────────────────────────────────────────────
log "5/9 claude CLI（本機 AI 引擎）"
if have claude; then
  ok "claude CLI 已安裝（$(command -v claude)）"
else
  warn "未偵測到 claude CLI，嘗試安裝官方版本…"
  curl -fsSL https://claude.ai/install.sh | bash || warn "claude 自動安裝失敗，可改用 ANTHROPIC_API_KEY 模式"
  export PATH="$HOME/.local/bin:$PATH"
  if have claude; then
    ok "claude CLI 安裝完成"
    warn "首次使用需登入：請執行  claude  並完成授權（一次即可）"
  else
    warn "claude CLI 不可用。平台仍可運作，但本機 AI 模式需 claude；或在 .env 設 ANTHROPIC_API_KEY 走雲端。"
  fi
fi

# ── 6. .env ───────────────────────────────────────────────────────────────────
log "6/9 .env 設定檔"
if [ -f .env ]; then
  ok ".env 已存在（保留現有設定）"
else
  cp .env.example .env
  warn "已從 .env.example 建立 .env —— 如需雲端 AI 或 mops 串接，請填入對應 key"
fi

# ── 7. 執行時目錄 ─────────────────────────────────────────────────────────────
log "7/9 建立 data/ logs/ 目錄"
mkdir -p data logs
ok "目錄就緒"

# ── 8. systemd user service ───────────────────────────────────────────────────
log "8/9 systemd user service（開機自啟 + crash 自動重啟）"
if have systemctl && systemctl --user show-environment >/dev/null 2>&1; then
  # 收集 node / claude 的 bin 目錄塞進 service PATH（subprocess 才找得到）
  EXTRA_PATH=""
  for b in claude node; do
    if have "$b"; then EXTRA_PATH="${EXTRA_PATH}$(dirname "$(command -v "$b")"):"; fi
  done
  SERVICE_DST="$HOME/.config/systemd/user/taiwan-company.service"
  mkdir -p "$(dirname "$SERVICE_DST")"
  sed -e "s|__REPO_DIR__|$REPO_DIR|g" \
      -e "s|__HOME__|$HOME|g" \
      -e "s|__EXTRA_PATH__|$EXTRA_PATH|g" \
      deploy/taiwan-company.service.template > "$SERVICE_DST"
  # 無頭 / SSH 登入也能開機自啟與背景常駐
  if [ -n "$SUDO" ] || [ "$(id -u)" -eq 0 ]; then
    $SUDO loginctl enable-linger "$USER" 2>/dev/null || true
  fi
  # 資料自動備份：oneshot service + 每日 timer（companies.json 等使用者資料）
  sed -e "s|__REPO_DIR__|$REPO_DIR|g" \
      deploy/taiwan-company-backup.service.template > "$HOME/.config/systemd/user/taiwan-company-backup.service"
  cp deploy/taiwan-company-backup.timer.template "$HOME/.config/systemd/user/taiwan-company-backup.timer"
  systemctl --user daemon-reload
  systemctl --user enable taiwan-company.service >/dev/null 2>&1 || true
  systemctl --user enable --now taiwan-company-backup.timer >/dev/null 2>&1 || true
  systemctl --user restart taiwan-company.service
  ok "service 已安裝並啟動（開機自啟 + 每日資料備份已開啟）"
else
  warn "systemd user instance 不可用，略過。可改用前景啟動： make start"
fi

# ── 9. healthcheck ────────────────────────────────────────────────────────────
log "9/9 healthcheck"
HEALTHY=""
for i in 1 2 3 4 5; do
  if curl -fsS http://localhost:8003/ >/dev/null 2>&1; then HEALTHY=1; break; fi
  sleep 2
done
if [ -n "$HEALTHY" ]; then
  ok "服務已就緒 → http://localhost:8003"
else
  warn "healthcheck 未通過。若用 systemd：journalctl --user -u taiwan-company -n 50"
  warn "或看 logs/app-error.log；也可改用前景啟動 make start 觀察錯誤"
fi

echo ""
ok "bootstrap 完成。"
echo "   - 瀏覽：http://localhost:8003"
echo "   - 若 claude CLI 是新裝的，記得先跑一次  claude  登入"
echo "   - 套用新 code： git pull && make restart"
