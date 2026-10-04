---
title: 資料流
status: living
last_updated: 2026-10-04
source_repo: ~/taiwan-company
---

# 資料流

## TL;DR

公司資料的生命週期：**上傳檔案 → AI 抽名單 → 使用者確認 → 自動補齊登記資料（enrich）→ 可選深度補資料（deep-enrich）→ 訪談 → 匯出報告**。所有狀態都落在 `data/companies.json` 單一檔案。

## 公司資料怎麼進來

1. **上傳**（`POST /api/upload`）— 接受 PDF / Word / Excel / 圖片。
   - 圖片走 AI Vision（`extract_companies_from_image`）
   - 文字檔走 `file_parser.extract_text`（PDF / DOCX / XLSX 用各自 lib，圖片型 PDF 走 tesseract OCR）
   - 長文本依換行邊界切成每批最多 8,000 字元，逐批交給 AI；結果依原順序合併去重，再分成 `valid` / `excluded` / `uncertain` 三組
2. **去重消歧**（`POST /api/companies/name-lookup`）— 對每個候選名稱呼叫 g0v ronnywang 搜尋 API，回最多 5 個候選給前端讓使用者挑（避免「台積電」vs「台積電股份有限公司」這種誤判）。
3. **確認**（`POST /api/companies/confirm`）— 使用者確認後寫進 `companies.json`，可選同時觸發 `_enrich_company` 背景任務。

## Enrich（基礎補資料）

`_enrich_company` 背景跑，靠 SSE（`GET /api/companies/enrich/{id}`）回報進度給前端：

1. 用名稱／統編查 g0v ronnywang → 補 `tax_id` / `capital` / `representative` / `address` / `directors` / `par_value` / `total_shares`
2. 名稱配 g0v 沒中時，fallback 查 GCIS App1 API（用 tax_id），多補 `setup_date` / `last_change_date` / `register_org`
3. 查 TWSE / TPEX / GISA 開放資料，標 `listing_status`（上市 / 上櫃 / 興櫃 / 創新板 / 非公發），24 小時 cache
4. AI 產一段公司簡介（`blurb` + `summary`）

## Deep-enrich（深度補資料）

`GET /api/companies/{id}/deep-enrich`，SSE。在基礎之上多跑：

- 法人董事辨識與母子公司關係圖（`build-relationship`）
- 大股東 / 公司簡介 / 專利三段折疊（commit fc7ada7）
- 串 `mops_investee` 反查公發母公司

## 匯出報告（DOCX / PDF）

`GET /api/companies/{id}/export?format=docx|pdf`，由 `services/company_exporter.py` 產出，視覺對齊 modal。涵蓋 modal 的完整資訊：**基本資料 → 董監事名單 → 大股東 → 公司簡介 → 專利**。

- 大股東段比照 modal `_renderShareholderSection`：董監事持股合計 < 99.9% 才顯示，列出未揭露比例提醒；並即時串 `mops_investee` 反查哪些公發公司揭露持有本公司股份（查不到不阻擋匯出）
- 專利段把 `company.patents` 列成表（專利號 / 名稱 / 申請日 / 狀態 / 發明人）
- **補充來源 callout**：公司簡介裡的「（簡報補充）／（訪談補充）／（介紹補充）／（筆記補充）」比照 modal `_supCallout` 渲染成來源著色的方塊（左側色條 + 底色 tint + 色標題），DOCX 用單格表格、PDF 用 filled rect；行內補充則著色文字。配色與 `static/style-*.css` 裡的 `.sup-*` 一致
- endpoint 為 async，匯出前先 await holders 反查再交給 exporter

## 母子公司關係圖

- `GET /api/companies/{id}/build-relationship` — SSE 串流建關係圖
- `GET /api/companies/{id}/ownership-graph` — 取現成關係圖
- `POST /api/companies/from-graph` — 從關係圖把節點直接加入公司列表
- 前端用 cytoscape.js 畫圖

## Call memo（訪談備忘錄）

完整工作流：

1. **上傳逐字稿**（`POST /memo/extract`）— 接受 TXT / Markdown / DOCX / PDF；Markdown 若有 `## 逐字稿`，會排除前置摘要，只分析原始逐字稿。原檔保存至 `data/uploads/{id}/`，metadata 寫入 `call_memo_source`
2. **或上傳音檔**（`POST /memo/transcribe-audio`）— 走 `whisper_transcriber`（本機 OpenAI Whisper），支援 MP3 / WAV / M4A / OGG / WEBM / FLAC
3. **AI 抽欄位**（`memo_extractor.extract_with_audit`）— 雙路抽取事實與逐字引用，原文驗證後由程式完整組裝 ~24 個欄位；evidence / coverage 另存 `data/memo_runs/{id}/`
4. **編輯儲存**（`PUT /memo`）
5. **下載 DOCX**（`GET /memo/download`）— 把欄位灌進 `data/call_memo_template.docx` 範本，輸出 `Call Memo-<公司名>_<日期>.docx`
6. **重跑抽取**（`POST /memo/reextract`）— 直接使用已保存的逐字稿來源，不必重新選檔；`GET /memo/source` 回傳檔名、大小、時間與下載 URL

Memo 欄位（從 `MemoSave` model 得知）：訪談日期、案源、受訪人、實收資本、地址、設立日、承銷商、簽證會計師、董事長、總經理、員工數、IPO 時程、投資條件、業務 / 營收、財務、經營團隊、董監持股、近期發展、主要客戶 / 供應商、產能、競爭者、產業趨勢、風險追蹤、結論。

## 競業關係

`routers/competitors.py`（邏輯在 `services/competitor_service.py`）維護公司之間的競業連結：

- `GET /{id}/competitor-graph` 取競業圖；`POST /{id}/competitors/add|remove` 手動增刪
- `POST /symmetrize-competitors`、`/relink-competitors`、`/backfill-competitors` 是批次維護（補成雙向、依名稱重連 id、補回缺漏），不經 UI 排程
- 簡介生成時 `gather_competitor_context` 會把已知競業併進 prompt

## 公司登記每股金額（findbiz）

`routers/findbiz.py` 用 Playwright 開真實瀏覽器（非 headless，需 DISPLAY）抓 findbiz.nat.gov.tw 的每股金額：`POST /api/findbiz/scrape` 啟動並回 `session_id` → 使用者手動通過 Cloudflare 後 `POST /confirm/{session_id}` → `GET /stream/{session_id}` 以 SSE 回報進度，結果寫回 `par_value` / `total_shares`。

## 產業地圖

`routers/industry_map.py` 依產業別呼叫 AI 生成產業鏈地圖（`GET /api/industry-map/{industry}/generate`，SSE），結果存 `data/industry_maps.json`。另有細分（`subdivide/propose` → `subdivide`）與合併（`merge`）：細分會改寫 `companies.json` 的產業標籤，寫入前先備份。`industry_maps.json` 由使用者手動觸發生成，排程器不會重生，所以納入每日備份。

## 新聞黑名單

`routers/news_blacklist.py`：前端新聞卡片的「不要這則」呼叫 `POST /api/news/dismiss`，每累積 5 筆自動請 AI 歸納過濾規則（`services/blacklist.py`，存 `data/blacklist.json`）。`GET /api/news/blacklist` 與 `POST /api/news/analyze` 是手動維運端點，刻意不接 UI。

## 每日新聞 digest

- 啟動時 lifespan 起一個排程 task：每天 08:00（台灣時間）之後依序跑 `refresh_all_digests`、`refresh_all_trends`
- **漏跑會補跑**：以 `data/scheduler_state.json` 的 `last_daily_run` 判斷今天是否已跑；機器在 08:00 關機，開機後（最久 1 小時內）自動補跑。跑完才記日期，中途被關掉下次會重跑
- 啟動時另有對帳：`reset_interrupted_jobs` 把上次中斷殘留的 `enrich_status: generating`、`materials_generating` 清掉
- 每個產業別獨立快取在 `daily_digest.json` / `industry_trends.json`，過 90 天自動 prune
- 新聞源：Google News RSS（`feedparser`），用產業同義詞擴展查詢；過濾中國媒體（人民日報、新華社等）
- AI 整理成「每日 digest」與「本季趨勢」，前端側欄按產業別呈現

## 公司資料 schema（`companies.json`）

實檔抽樣後的欄位：

```yaml
id: UUID
name: 完整公司名（含「股份有限公司」）
tax_id: 8 碼統編
labels: [標籤陣列, 例: "綠色配投", "創業大聯盟決賽2026"]
industry: 產業別字串（例: "循環經濟"）
group: 群組
listing_status: 上市 / 上櫃 / 興櫃 / 創新板 / 公發 / 非公發
capital: 實收資本（元）
authorized_capital: 資本總額
representative: 負責人
par_value: 每股面額
total_shares: 已發行股數
directors:
  - name, title, representative_of（法人代表的母公司，自然人為 ""）, shares, ratio
address: 登記地址
setup_date / last_change_date / register_org: GCIS 補的
blurb: 一句話簡介
summary: AI 產的長段 Markdown 分析（業務概況 / 競業分析 / ...）
watched: bool（追蹤旗標）
call_memo: { ...Memo 欄位 }
call_memo_source: { filename, stored_name, url, size, uploaded_at }（最新逐字稿來源；原檔落地 data/uploads/{id}/）
call_memo_last_run: { source_sha256, engine, model, prompt_version, chunk_count, fields, ... }
call_memo_runs: [ ... ]（最近 20 次 Call Memo 執行紀錄，供比對引擎與結果）
patents: [...]（deep-enrich 後有）
materials: [ { filename, stored_name, url, mime_type, size, uploaded_at } ]（上傳的簡報/介紹/照片，落地在 data/uploads/{id}/，由 /uploads 提供存取）
materials_summary: 由上傳簡報用 Opus（最新）生成的簡報版簡介（暫存，供逐段審核用）
materials_blurb: 簡報簡介的一句話
materials_generated_at: 簡報簡介生成時間 ISO timestamp
materials_applied_headings: [頂層段落標題]（summary 中含簡報內容的頂層段落，通常是「營運綜覽」與被取代的「業務概況」，前端據此標「簡報」chip；整份重新生成 summary 時會清空）
enrich_status: "" / ok / failed / generating（generating 只會出現在任務執行中，啟動時殘留的會被清掉）
enrich_error: 失敗原因（enrich_status 為 failed 時）
enrich_warning: 簡介生成成功、但政府登記資料查無或失敗時的提示（卡片顯示「登記資料未更新」）；成功時為空字串
last_updated: ISO timestamp
```

## 備份

`taiwan-company-backup.timer` 每天 03:30 打包 `companies.json`、`config.json`、`industry_keywords.json`、`blacklist.json`、`industry_maps.json` 與 `uploads/`、`memo_runs/`；純快取（digest、trends、`listing_cache.json`）不備份。

## 相關

- [[architecture]]
- [[ai-features]]
- [[integration]]
