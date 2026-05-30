---
title: 資料流
status: living
last_updated: 2026-05-29
source_repo: ~/taiwan-company
---

# 資料流

## TL;DR

公司資料的生命週期：**上傳檔案 → AI 抽名單 → 使用者確認 → 自動補齊登記資料（enrich）→ 可選深度補資料（deep-enrich）→ 訪談 → 匯出報告**。所有狀態都落在 `data/companies.json` 單一檔案。

## 公司資料怎麼進來

1. **上傳**（`POST /api/upload`）— 接受 PDF / Word / Excel / 圖片。
   - 圖片走 AI Vision（`extract_companies_from_image`）
   - 文字檔走 `file_parser.extract_text`（PDF / DOCX / XLSX 用各自 lib，圖片型 PDF 走 tesseract OCR）
   - AI 把文本拆出公司清單，分成 `valid` / `excluded` / `uncertain` 三組
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
- endpoint 為 async，匯出前先 await holders 反查再交給 exporter

## 母子公司關係圖

- `GET /api/companies/{id}/build-relationship` — SSE 串流建關係圖
- `GET /api/companies/{id}/ownership-graph` — 取現成關係圖
- `POST /api/companies/from-graph` — 從關係圖把節點直接加入公司列表
- 前端用 cytoscape.js 畫圖

## Call memo（訪談備忘錄）

完整工作流：

1. **上傳逐字稿**（`POST /memo/extract`）— 接受 .txt / DOCX / PDF，走 `file_parser` 抽文字
2. **或上傳音檔**（`POST /memo/transcribe-audio`）— 走 `whisper_transcriber`（本機 OpenAI Whisper），支援 MP3 / WAV / M4A / OGG / WEBM / FLAC
3. **AI 抽欄位**（`memo_extractor.extract_from_transcript`）— 把逐字稿映射到 ~24 個結構化欄位（見下）
4. **編輯儲存**（`PUT /memo`）
5. **下載 DOCX**（`GET /memo/download`）— 把欄位灌進 `data/call_memo_template.docx` 範本，輸出 `Call Memo-<公司名>_<日期>.docx`

Memo 欄位（從 `MemoSave` model 得知）：訪談日期、案源、受訪人、實收資本、地址、設立日、承銷商、簽證會計師、董事長、總經理、員工數、IPO 時程、投資條件、業務 / 營收、財務、經營團隊、董監持股、近期發展、主要客戶 / 供應商、產能、競爭者、產業趨勢、風險追蹤、結論。

## 每日新聞 digest

- 啟動時 lifespan 起兩個排程：08:00 跑 `refresh_all_digests`、08:05 跑 `refresh_all_trends`
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
patents: [...]（deep-enrich 後有）
materials: [ { filename, stored_name, url, mime_type, size, uploaded_at } ]（上傳的簡報/介紹/照片，落地在 data/uploads/{id}/，由 /uploads 提供存取）
materials_summary: 由上傳簡報用 opus-4.7 生成的簡報版簡介（暫存，供逐段審核用）
materials_blurb: 簡報簡介的一句話
materials_generated_at: 簡報簡介生成時間 ISO timestamp
materials_applied_headings: [頂層段落標題]（summary 中含簡報內容的頂層段落，通常是「營運綜覽」與被取代的「業務概況」，前端據此標「簡報」chip；整份重新生成 summary 時會清空）
last_updated: ISO timestamp
```

## 相關

- [[architecture]]
- [[ai-features]]
- [[integration]]
