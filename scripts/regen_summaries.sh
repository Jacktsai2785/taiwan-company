#!/usr/bin/env bash
# 批次重新生成公司簡介（普通生成 / Sonnet，序列化）。
# 自給自足：每次從現況即時算「尚需重生成」清單（非新版5欄競業表、非補充資料、不在 skip-list）。
#   → 完成的會變 new5 自動排除；/tmp 被清也不影響（狀態在 companies.json + logs/）。
# 韌性：偵測到 Claude 用量上限 → 等待後重試同一間；非上限失敗 3 次 → 加入 skip-list。
# 狀態檔放 repo logs/（持久）。進度：logs/regen_progress.log
set -u
cd /home/jacktsai/taiwan-company || exit 1
BASE="http://localhost:8003"
LOG=logs/regen_progress.log
SKIP=logs/regen_skip.txt
PY=.venv/bin/python
WAIT_LIMIT=900     # 偵測到上限時等待秒數（15 分）後重試
touch "$SKIP"

mapfile -t ROWS < <("$PY" - "$SKIP" <<'PYEOF'
import sys, re
from services import data_store
try:
    skip = {l.strip() for l in open(sys.argv[1]) if l.strip()}
except FileNotFoundError:
    skip = set()

def need(c):
    s = c.get("summary") or ""
    if not s.strip(): return True
    m = re.search(r"^##\s*競業分析([\s\S]*?)(?=^##|\Z)", s, re.M)
    if not m: return True
    hdr = re.search(r"^\|\s*公司名稱.*\|", m.group(1), re.M)
    if not (hdr and "競業類型" in hdr.group(0)): return True   # legacy 4 欄
    for ln in m.group(1).splitlines():                          # 5 欄但多公司同格
        ln = ln.strip()
        if not ln.startswith("|") or "---" in ln: continue
        cells = [x.strip() for x in ln.strip("|").split("|")]
        nm = cells[0] if cells else ""
        nm_top = re.sub(r"（[^）]*）|\([^)]*\)", "", nm)           # 去掉括號內容（產品名等）再判斷
        if nm not in ("公司名稱", "") and "本案" not in nm and re.search(r"[、／/]|與", nm_top): return True
    return False

EXCLUDE_LABELS  = {"潛在案源"}                              # 不重生成
PRIORITY_LABELS = {"創業大聯盟決賽2026", "創業大聯盟複賽2026"}  # 優先（無標籤者也算）
PRIORITY_ONLY   = True   # ← 使用者要求：只跑優先，跑到 AAMA 就停（之後改 False 即續跑 AAMA）
for c in data_store.get_all_companies():
    if c["id"] in skip: continue
    if c.get("materials_applied_headings") or c.get("materials_summary"): continue
    labels = set(c.get("labels") or [])
    if EXCLUDE_LABELS & labels: continue
    if not need(c): continue
    is_prio = bool(PRIORITY_LABELS & labels) or not labels
    if PRIORITY_ONLY and not is_prio: continue   # AAMA 等非優先暫緩
    print(f'{c["id"]}\t{c["name"]}')
PYEOF
)
TOTAL=${#ROWS[@]}
echo "=== 批次開始：尚需 $TOTAL 間（$(date '+%F %T')）===" >> "$LOG"

is_limit() { grep -qiE "limit reached|usage limit|rate limit|will reset|resets|額度|流量上限|用量上限|請稍後再試|too many requests|overloaded|429|529" ; }

i=0; ok=0; fail=0
for row in "${ROWS[@]}"; do
  i=$((i+1)); id="${row%%	*}"; name="${row#*	}"
  (( (i-1)%5==0 )) && echo "--- 批次 $(( (i-1)/5+1 )) ---" >> "$LOG"
  failN=0; out=""
  while :; do
    echo "[$i/$TOTAL] ▶ $name 生成中…（$(date '+%T')）" >> "$LOG"
    out=$(curl -sN --max-time 1000 "$BASE/api/companies/$id/summarize" 2>/dev/null)
    if echo "$out" | grep -q "公司簡介已生成完成"; then break; fi
    if echo "$out" | is_limit; then
      echo "[$i/$TOTAL] ⏸ 偵測到用量上限，等待 $((WAIT_LIMIT/60)) 分鐘後重試 $name（$(date '+%T')）" >> "$LOG"
      sleep "$WAIT_LIMIT"; continue
    fi
    failN=$((failN+1))
    if [ "$failN" -ge 3 ]; then
      echo "$id" >> "$SKIP"
      echo "[$i/$TOTAL] ✗ $name 連續失敗 ${failN} 次 → 加入 skip。末段：$(echo "$out" | tr '\n' ' ' | tail -c 160)" >> "$LOG"
      fail=$((fail+1)); break
    fi
    echo "[$i/$TOTAL] ↻ $name 失敗（第 ${failN} 次），10 秒後重試…" >> "$LOG"
    sleep 10
  done
  if echo "$out" | grep -q "公司簡介已生成完成"; then
    ok=$((ok+1)); echo "[$i/$TOTAL] ✓ $name（$(date '+%T')）" >> "$LOG"
  fi
done
echo "=== 完成：成功 $ok / 失敗 $fail / 共 $TOTAL（$(date '+%F %T')）===" >> "$LOG"
