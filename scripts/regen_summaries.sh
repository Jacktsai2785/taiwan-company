#!/usr/bin/env bash
# 批次重新生成公司簡介（普通生成 / Sonnet，序列化）。
# 韌性：偵測到 Claude 用量上限 → 暫停等待、解除後重試同一間（不跳過）。
# 可續跑：已完成的 id 記在 done-file，重啟會自動略過。
# 目標清單：/tmp/regen_targets.json（已排除 has_materials 的公司）。
# 進度：/tmp/regen_progress.log
set -u
cd /home/jacktsai/taiwan-company || exit 1
BASE="http://localhost:8003"
LOG=/tmp/regen_progress.log
DONE=/tmp/regen_done.txt
PY=/home/jacktsai/taiwan-company/.venv/bin/python
WAIT_LIMIT=900     # 偵測到上限時等待秒數（15 分）後重試
touch "$DONE"

mapfile -t ROWS < <("$PY" - <<'PYEOF'
import json
for t in json.load(open("/tmp/regen_targets.json")):
    if not t["has_materials"]:
        print(f'{t["id"]}\t{t["name"]}')
PYEOF
)
TOTAL=${#ROWS[@]}
echo "=== 批次（含 limit 自動等待）開始：共 $TOTAL 間（$(date '+%F %T')）===" >> "$LOG"

# 判斷一段文字是否像「用量／流量上限」
is_limit() { grep -qiE "limit reached|usage limit|rate limit|will reset|resets|額度|流量上限|用量上限|請稍後再試|too many requests|overloaded|529|429" ; }

i=0; ok=0; fail=0
for row in "${ROWS[@]}"; do
  i=$((i+1))
  id="${row%%	*}"; name="${row#*	}"
  if grep -qx "$id" "$DONE"; then
    echo "[$i/$TOTAL] ⏭ $name 已完成，略過" >> "$LOG"; ok=$((ok+1)); continue
  fi
  (( (i-1)%5==0 )) && echo "--- 批次 $(( (i-1)/5+1 )) ---" >> "$LOG"

  failN=0; out=""
  while :; do
    echo "[$i/$TOTAL] ▶ $name 生成中…（$(date '+%T')）" >> "$LOG"
    out=$(curl -sN --max-time 1000 "$BASE/api/companies/$id/summarize" 2>/dev/null)
    if echo "$out" | grep -q "公司簡介已生成完成"; then break; fi
    if echo "$out" | is_limit; then
      echo "[$i/$TOTAL] ⏸ 偵測到 Claude 用量上限，等待 $((WAIT_LIMIT/60)) 分鐘後重試 $name（$(date '+%T')）" >> "$LOG"
      sleep "$WAIT_LIMIT"; continue
    fi
    failN=$((failN+1))
    if [ "$failN" -ge 3 ]; then
      echo "[$i/$TOTAL] ✗ $name 連續失敗 ${failN} 次，跳過。末段：$(echo "$out" | tr '\n' ' ' | tail -c 180)" >> "$LOG"
      fail=$((fail+1)); break
    fi
    echo "[$i/$TOTAL] ↻ $name 失敗（第 ${failN} 次），10 秒後重試…" >> "$LOG"
    sleep 10
  done

  if echo "$out" | grep -q "公司簡介已生成完成"; then
    verdict=$("$PY" - "$id" <<'PYEOF'
import sys, re
from services import data_store
c = data_store.get_company(sys.argv[1]); s = (c or {}).get("summary") or ""
if not s.strip(): print("WARN(無summary)"); sys.exit()
m = re.search(r"^##\s*競業分析([\s\S]*?)(?=^##|\Z)", s, re.M)
hdr = re.search(r"^\|\s*公司名稱.*\|", m.group(1), re.M) if m else None
five = ("競業類型" in hdr.group(0)) if hdr else False
multi = 0
if m:
    for ln in m.group(1).splitlines():
        ln = ln.strip()
        if not ln.startswith("|") or "---" in ln: continue
        cells = [x.strip() for x in ln.strip("|").split("|")]
        if not cells: continue
        nm = cells[0]
        if nm in ("公司名稱", "") or "本案" in nm: continue
        if re.search(r"[、／/]|與", nm): multi += 1
print(f"OK(5欄={five}, 殘留多公司={multi})")
PYEOF
)
    echo "$id" >> "$DONE"; ok=$((ok+1))
    echo "[$i/$TOTAL] ✓ $name → $verdict（$(date '+%T')）" >> "$LOG"
  fi
done
echo "=== 完成：成功 $ok / 失敗 $fail / 共 $TOTAL（$(date '+%F %T')）===" >> "$LOG"
