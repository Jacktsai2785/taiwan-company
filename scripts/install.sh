#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# scripts/install.sh — 遠端一鍵安裝入口。
#
# 用法（在任何新裝置，不需先 clone）：
#     curl -fsSL https://raw.githubusercontent.com/Jacktsai2785/taiwan-company/main/scripts/install.sh | bash
#
# 可用環境變數覆寫：
#     REPO_URL    來源 repo（預設 Jacktsai2785/taiwan-company）
#     TARGET_DIR  安裝目錄（預設 $HOME/taiwan-company）
#     BRANCH      分支（預設 main）
#
# 流程：確保 git → clone 或 pull → 交給 scripts/bootstrap.sh 裝完整環境。
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Jacktsai2785/taiwan-company.git}"
TARGET_DIR="${TARGET_DIR:-$HOME/taiwan-company}"
BRANCH="${BRANCH:-main}"

log()  { printf '\n\033[1;36m── %s ──\033[0m\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

# git（clone 必要）
if ! have git; then
  log "安裝 git"
  if have apt-get; then
    if [ "$(id -u)" -ne 0 ] && have sudo; then sudo apt-get update -qq && sudo apt-get install -y git;
    elif [ "$(id -u)" -eq 0 ]; then apt-get update -qq && apt-get install -y git;
    else echo "需要 git，請先安裝後重試"; exit 1; fi
  else
    echo "找不到 git，且非 apt 系統，請手動安裝 git 後重試"; exit 1
  fi
fi

# clone 或更新
if [ -d "$TARGET_DIR/.git" ]; then
  log "更新既有 repo：$TARGET_DIR"
  git -C "$TARGET_DIR" fetch --quiet origin "$BRANCH"
  git -C "$TARGET_DIR" checkout --quiet "$BRANCH"
  git -C "$TARGET_DIR" pull --quiet --ff-only origin "$BRANCH"
else
  log "Clone $REPO_URL → $TARGET_DIR"
  git clone --branch "$BRANCH" "$REPO_URL" "$TARGET_DIR"
fi

# 交棒給 bootstrap
log "執行 bootstrap（安裝完整環境）"
bash "$TARGET_DIR/scripts/bootstrap.sh"
