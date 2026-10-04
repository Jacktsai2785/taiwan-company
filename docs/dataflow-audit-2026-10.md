---
title: 資料流稽核
status: living
last_updated: 2026-10-04
source_repo: ~/taiwan-company
---

# 資料流稽核（2026-10-04）

**結論先講**：公司資料的「單一寫入點 + 原子寫 + 鎖」是真的，而且守得很好；真正的落差在兩處不報錯的地方：每日新聞排程在機器關機時整天漏跑且不補跑，以及 42 家公司永遠卡在 `enrich_status: "generating"`。

## 宣稱的流程

來源：[data-flow.md](data-flow.md)（last_updated 2026-08-05）與專案 CLAUDE.md。
宣稱：所有狀態落在 `data/companies.json` 單一檔案；AI 引擎只由 server config 決定；每日 08:00 / 08:05 排程新聞；每日備份當安全網。

## 實際的流程與反證結果

| 宣稱 | 反證做法 | 結果 | 證據 |
|---|---|---|---|
| companies.json 只有一個寫入者 | grep 所有寫入點 | 🟢 成立。只有 `data_store._write`，其他小檔也共用 `write_json` | `services/data_store.py:53,75-76` |
| 多執行緒不會 lost update | 臨時檔上 16 執行緒 x 25 次 `update_company` | 🟢 預期 400 欄位、實得 400 | probe（臨時檔，未動真實資料） |
| `get_company → upsert` 會過期覆寫 | 兩份舊副本先後 upsert | 🟡 確實會丟掉先寫的欄位，但 `upsert_company` 只被 `data_store` 內部呼叫，外部一律走 merge 式 `update_company` | `data_store.py:619-668`，grep 無外部呼叫 |
| 引擎只由 server 設定決定 | 傳 `codex` / `gemini` 給 header 與 query | 🟢 一律回 `claude`，請求覆寫無效 | `services/ai_deps.py:11-14` |
| 前端呼叫的端點都存在 | 擷取 55 條 `/api/` 字串比對 74 條路由 | 🟢 無缺漏（2 條為字串模板擷取誤報） | 比對腳本 |
| MOPS 被投資反查可用 | 實打 8085 與 8080 | 🟢 8085 正常（台積電回 1 筆）；🔴 `~/PORTS.md` 寫 8080，與實況不符 | `.env:3`、`ss -ltn` |
| 每日 08:00 排程新聞 | 查 digest 最新日期與服務啟停時間 | 🔴 最新資料停在 10-02；服務 10-03 00:02 停、10-04 14:39 才起，期間 08:00 沒人補跑 | `main.py:43-52`、`data/daily_digest.json` |
| enrich 完成會標狀態 | 統計真實資料 | 🔴 42 家卡在 `generating`（203 ok、475 無狀態） | `routers/enrichment.py:538,593,605` |
| 政府登記資料失敗使用者看得到 | 讀失敗分支 | 🟡 只在進度訊息出現一次，且最後仍寫 `enrich_status: "ok"`，事後查不到 | `routers/enrichment.py:576-578,593` |

## 修復狀態（2026-10-04）

| # | 落差 | 狀態 | 做法與驗證 |
|---|---|---|---|
| 1 | 排程不補跑 | 已修 | `main.py` 改以 `data/scheduler_state.json` 的 `last_daily_run` 判斷，最久 1 小時內重判；重啟後實測自動補跑今天的 digest |
| 2 | 42 家 `generating` 孤兒 | 已修 | 啟動時 `reset_interrupted_jobs()` 對帳；實測 42 家已清空（有簡介者歸回無狀態） |
| 3 | 政府資料失敗標 ok | 已修 | 新增 `enrich_warning` 欄位，卡片顯示「⚠ 登記資料未更新」 |
| 4 | 備份漏 industry_maps | 已修 | `scripts/backup_data.sh` 納入；舊註解說「排程器會重生」是錯的，已更正 |
| 5 | API 有 UI 沒接 | 已處理 | 刪除無人使用的 `/api/config/groups`（前端自己算群組）；`/api/news/blacklist`、`/analyze` 為刻意保留的手動維運端點，已寫入文件 |
| 6 | 文件缺漏 | 已修 | `data-flow.md` 補競業、findbiz、產業地圖、新聞黑名單、排程補跑、備份，並更正 `style.css` 說法 |
| 7 | `~/PORTS.md` 寫錯 | 未動 | 在 repo 外，需另行修改 |

驗證：全部 105 個測試通過（新增 `tests/test_dataflow_gaps.py` 11 個）。

## 落差明細（修復前）

1. **每日排程不補跑**（🔴）。`_daily_scheduler` 只會睡到下一個 08:00，不看「上次成功是何時」。WSL 或機器在 08:00 關著，當天就沒有 digest，也沒有任何錯誤。
2. **42 家 `generating` 孤兒**（🔴）。啟動時沒有人把殘留的 `generating` 重設（重啟或 crash 會留下）。目前影響小：這些公司都有 summary，只有 `_company_merge_rank` 會把它當「未完成」。但狀態欄位已不可信。
3. **政府資料抓取失敗被標成 ok**（🟡）。使用者無法事後分辨「簡介有生成但登記資料沒更新」。
4. **備份漏 `industry_maps.json`**（🟡，163 KB，AI 生成、重做要花額度）。`scripts/backup_data.sh:23` 的 FILES 只含 companies / config / industry_keywords / blacklist。是否算「可重建」要你決定。
5. **API 有、前端沒接**（🟡）：`/api/config/groups`、`/api/news/blacklist`、`/api/news/analyze` 在 static 找不到字串引用（也可能是動態組字串，未逐一確認）。
6. **文件缺漏**（🟡）：`findbiz`、`industry-map`、`competitors`、`news/blacklist` 四組路由完全沒出現在 docs；`data-flow.md` 還寫 `style.css`（現已拆成 8 檔）。
7. **`~/PORTS.md` 寫錯**：mops_investee 實際在 8085。此檔在 repo 外，我沒改。

## 好消息（不要動壞的東西）

- `data_store` 的 RLock、mtime 快取、tmp + `os.replace` 原子寫是對的，併發測試實證。不要為了「簡化」拆掉。
- `ai_deps` 讓請求無法覆寫引擎，符合 CLAUDE.md 的 SSOT 設計。
- 每日排程內部有逐產業 try/except 與 1 小時有界重試，單一產業失敗不拖垮整體。
- regen 腳本的狀態來自 companies.json 現況實算，不靠 `enrich_status`，所以孤兒 `generating` 不會讓它重做或漏做。
- 資料完整性：720 家公司無重複 id / 名稱 / 統編。

## 建議釘住的接縫

- **Tracer bullet**：一筆測試公司走 confirm → enrich（GCIS mock）→ 寫入 → 重讀，每站 assert。
- **排程補跑**：模擬「最新 digest 日期 < 今天且已過 08:00」，啟動時應補跑。
- **孤兒狀態**：啟動後 `generating` 且無執行中任務者應被重設。

## 未做的部分

- 功能關係圖（HTML 主表）與儲存層圖**這次沒有產出**，只有本文的文字版對帳。
- 沒有「拔依賴」實測（停 mops、斷 GCIS）；原因是服務是你日常在用的單機正式環境，我不在真實資料上做破壞性實驗。call memo、上傳、材料、競業鏈這幾條流程只做了靜態對帳，沒有逐條反證。

## 接手要先決定的事

1. 排程補跑要做嗎？做的話，啟動時補一次，還是改成 systemd timer（`Persistent=true`，跟備份同一套）？
2. `generating` 孤兒：啟動時自動重設，還是接受它只是個標籤？
3. `industry_maps.json` 要不要納入每日備份？
