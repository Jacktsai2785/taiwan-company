#!/usr/bin/env bash
# 台灣產業商情平台 — 使用者資料自動備份
#
# 備份「手工累積、無法重建」的資料檔（companies.json 等），略過排程器會重生的快取。
# 設計重點：
#   - 內容沒變就跳過：用 companies.json 的 sha256 比對上次，閒置日不堆重複檔
#   - 落地前先驗證 JSON 可解析：避免把寫到一半的壞檔存成「備份」（data_store 非原子寫）
#   - gzip 壓縮 + 自動輪替：只保留最近 N 份
#   - flock 單實例、冪等：可被 systemd timer / make backup / 手動安全重複呼叫
#
# 可用環境變數覆寫：
#   BACKUP_DIR   備份存放目錄（預設 ~/taiwan-company-backups）
#   BACKUP_KEEP  保留份數（預設 30）
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="$REPO_ROOT/data"
BACKUP_DIR="${BACKUP_DIR:-$HOME/taiwan-company-backups}"
KEEP="${BACKUP_KEEP:-30}"

# 要備份的「使用者資料」（手工累積、無法重建）。快取類（daily_digest / industry_trends /
# industry_maps / migrate_progress）不備份——排程器會重生。
FILES=(companies.json config.json industry_keywords.json blacklist.json)

mkdir -p "$BACKUP_DIR"

# 單實例：systemd timer 與手動執行不疊跑
exec 9>"$BACKUP_DIR/.backup.lock"
flock -n 9 || { echo "另一個備份程序執行中，跳過"; exit 0; }

SRC="$DATA_DIR/companies.json"
[ -f "$SRC" ] || { echo "找不到 $SRC，跳過（尚無資料）"; exit 0; }

# 1) 驗證 companies.json 可解析為 JSON（壞檔/寫到一半不備份）
PY="$REPO_ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="python3"
if ! "$PY" -c "import json,sys; json.load(open(sys.argv[1], encoding='utf-8'))" "$SRC" 2>/dev/null; then
  echo "⚠ companies.json 無法解析為 JSON（可能正在寫入），本次跳過備份" >&2
  exit 1
fi

# 2) 內容沒變就跳過
HASH="$(sha256sum "$SRC" | cut -d' ' -f1)"
HASH_FILE="$BACKUP_DIR/.last_companies.sha256"
if [ -f "$HASH_FILE" ] && [ "$(cat "$HASH_FILE")" = "$HASH" ]; then
  echo "companies.json 自上次備份未變更，跳過"
  exit 0
fi

# 3) 打包 + 壓縮
TS="$(date +%Y%m%d_%H%M%S)"
ARCHIVE="$BACKUP_DIR/taiwan-company-data_$TS.tar.gz"
EXISTING=()
for f in "${FILES[@]}"; do
  [ -f "$DATA_DIR/$f" ] && EXISTING+=("$f")
done
tar -czf "$ARCHIVE" -C "$DATA_DIR" "${EXISTING[@]}"
echo "$HASH" > "$HASH_FILE"
echo "✅ 已備份 ${#EXISTING[@]} 檔 → $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))"

# 4) 輪替：只留最近 N 份
mapfile -t ALL < <(ls -1t "$BACKUP_DIR"/taiwan-company-data_*.tar.gz 2>/dev/null || true)
if [ "${#ALL[@]}" -gt "$KEEP" ]; then
  for old in "${ALL[@]:$KEEP}"; do
    rm -f "$old" && echo "🗑 輪替刪除 $old"
  done
fi
