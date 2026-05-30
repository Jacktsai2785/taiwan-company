---
title: AI 功能清單
status: living
last_updated: 2026-05-29
source_repo: ~/taiwan-company
---

# AI 功能清單

## TL;DR

平台幾乎每個寫入動作背後都有 AI：抽取公司名、分產業、產簡介、寫摘要、抽 call memo 欄位、整理新聞、推趨勢。AI 呼叫統一走 `services/claude_client.ask()`，由它決定打 Anthropic / OpenAI / Gemini / 本機 CLI。

## 核心統一入口

`services/claude_client.py` 提供三個公開函式：

- `ask(prompt, allowed_tools, api_key, provider, model)` — 文字 prompt
- `ask_with_image(prompt, image_content, suffix, api_key, provider)` — 單張圖多模態（Vision）
- `ask_with_files(prompt, file_paths, api_key, provider, model)` — 多檔多模態（PDF + 多張圖）。本機 CLI 模式用 `--add-dir` + Read tool 原生讀取；API 模式把 PDF 當 document block、圖片當 image block 一次送出。Office/txt 文件由呼叫端先抽文字內嵌進 prompt。

呼叫優先順序：

1. 若沒帶 `api_key`，讀 env `ANTHROPIC_API_KEY`
2. 若仍沒有 → 走本機 `claude` CLI（`subprocess.run`）
3. 若有 key → 依 `provider` 分支：`anthropic` / `openai` / `gemini`

`allowed_tools` 含 `WebSearch` / `WebFetch` 時，Anthropic 走 web search tool（`web_search_20250305`），OpenAI 切到 `gpt-4o-search-preview`，Gemini 啟用 `google_search`。

預設模型透過環境變數覆寫：`CLAUDE_MODEL` / `OPENAI_MODEL` / `GEMINI_MODEL`。

錯誤訊息特別處理：401/403 給「Key 無效」、429 給「達流量上限」、Gemini blockReason 也轉成中文提示，前端能直接顯示給使用者。

## AI 用在哪些地方

依 `services/` 反推：

### 1. 公司清單抽取（`company_extractor.py`）
- `extract_companies_from_text` — 從 PDF / Word / Excel 抽出的純文字裡，請 AI 判斷哪些字串是「真實公司名」，分 `valid` / `excluded` / `uncertain` 三組
- `extract_companies_from_image` — Vision 直接看圖辨識公司名（適合掃描檔、簡報截圖）
- `suggest_industries_for_companies` — 給一批公司 + 候選產業別，AI 回每家公司的建議分類
- `suggest_companies_for_industry` — 反向：給產業別與既有名單，建議該產業內值得追蹤的公司

### 2. 公司簡介與深度分析（enrich / deep-enrich）
- 在 `_enrich_company` / `_deep_enrich_company` 任務中產出 `blurb`（一句話）與 `summary`（長段 Markdown，含業務概況 / 競業分析 / SWOT 之類）
- 從實檔 sample 看到的 summary 結構：`## 業務概況` + `## 競業分析`（含表格）+ 其他段

### 2b. 補充資料 → 增強公司簡介（materials，`routers/materials.py` + `report_generator.generate_summary_from_materials`）
- **統一「📎 補充資料」側欄**：把原本分開的「簡報摘要」與「訪談備忘錄」併成單一面板，集中所有補充來源：①上傳檔案（簡報/介紹/照片，落地 `data/uploads/{id}/`、`/uploads` serve、可點開）②訪談備忘錄（24 欄可手動 key，或上傳逐字稿/錄音自動填，保留 DOCX 匯出）
- 點「✦ 用 Opus 4.7 更新公司簡介」後（前端 spinner + 計時動畫），後端把 PDF/圖片交給 `ask_with_files`、office/txt 抽文字內嵌、訪談備忘錄由 `serialize_memo` 組成訪談文字，一起用 `claude-opus-4-7`（`_DEEP_MODEL`）讀過，生成整合版簡介，存 `materials_summary` / `materials_blurb`
- **依來源標註補充**：prompt 要求新增/補充內容依來源標「（簡報補充）」（上傳檔案）或「（訪談補充）」（訪談備忘錄）；前端 `renderSummary` 把這些補充渲染成**可摺疊的 callout 區塊、依來源著色**（簡報 teal / 訪談 紫 / 介紹 琥珀 / 筆記 灰）
- 生成完跳出**逐段審核框**：把簡報版按 `##` 拆段，使用者勾選後 `POST /materials/apply` 合併進公開的 `summary`。合併規則：deck 段落若同名於公開 DD 段落（業務概況/競業分析/主要風險）→ 就地取代該段（標「修改」）；其餘 deck 主題（產品與服務、商業模式、團隊、財務、觀察…）→ 一律收進單一上層 `## 營運綜覽`，各為 `### 子段`（標「歸入營運綜覽」）
- 被套用的頂層段落（營運綜覽、被取代的業務概況）記在 `materials_applied_headings`，前端 `renderSummary` 據此把標題用 teal 字 + 尾綴 📎 標示（不再用 teal 盒子）
- 公司簡介**所有 `##` 段落一律可摺疊**（前端 `applyCollapsible` 不再用 hardcoded 白名單），不論產出什麼標題都不必改 code
- 一旦走「重新生成 / 深度生成」整份重建 `summary`，`_save_summary_result` 會清空 `materials_applied_headings`（標記不再適用）
- prompt 嚴格限定「只寫補充資料明確出現的資訊、禁杜撰數字、查無則標『——（補充資料未提供）』」
- **業務概況與主要風險整合**：生成時把現有公開的「業務概況」「主要風險」一併餵給 Opus，要求**完整保留既有內容 + 補充檔案或訪談讀到的額外資訊/風險**（依來源標「（簡報補充）」「（訪談補充）」），輸出單一整合段；亮點獨立成「## 投資亮點」收進營運綜覽。風險因此只集中一處

### 3. Call memo 抽取（`memo_extractor.py`）
- `extract_from_transcript(company_name, transcript)` — 把訪談逐字稿映射到 24 個結構化欄位（受訪人、財務、客戶、風險、結論…）
- 配合 `whisper_transcriber.py` 走「音檔 → 逐字稿 → 結構化欄位」的全自動流程

### 4. 每日新聞 digest（`daily_digest.py`）
- 排程：每天 08:00 跑全部產業的 digest，08:05 跑趨勢
- 流程：先用 `news_fetcher` 抓 Google News RSS（用產業同義詞擴展查詢、濾掉中國媒體），再請 AI 整理成「重點新聞 / 摘要 / 標題與連結」
- 也有 `WATCHLIST_TOPIC = "感興趣名單"` — 對打標 `watched` 的公司另開一個 digest 主題
- 90 天自動 prune

### 5. 產業關鍵字推薦（`config/industries/suggest`）
- 給定產業別，AI 推薦該產業的同義詞 / 子主題，存 `industry_keywords.json`，回頭給 `news_fetcher` 用來擴展 Google News 查詢

### 6. 母子公司關係圖
- 法人董事辨識本身是 rule-based（看 `representative_of` 欄），但**圖摘要**與**法人關係解讀**走 AI
- 結合 `mops_investee` 反查結果做交叉驗證

## 雙模式切換邏輯

- 平台啟動時先看 `ANTHROPIC_API_KEY` 環境變數（雲端部署必設）
- UI 右上角 ⚙ 開設定面板，使用者可選 provider + 輸入 Key，存 `localStorage`
- 每次 API 請求由 `ai_from_headers` 從 `X-API-Key` / `X-AI-Provider` 取出，傳進 router → service
- 若兩者都沒，走本機 CLI；CLI 也找不到時回友善訊息「請設 Key 或安裝 Claude Desktop」

## 本機 CLI 探尋路徑（`_find_cli`）

依序找：

1. `CLAUDE_CLI_PATH` env
2. `shutil.which("claude")` / `claude.exe`
3. Bun cache：`~/.bun/install/cache/@anthropic-ai/claude-{agent-sdk,code}-win32-x64/<version>/claude.exe`
4. gstack skills：`~/.claude/skills/gstack/node_modules/**/claude{,.exe}`

支援 Windows / Linux / macOS 的混合環境。

## 已知限制

- Claude CLI 模式單次呼叫最長 timeout 預設 120 秒，長 prompt 容易超時
- Gemini 在某些 prompt 下會回 `finishReason=SAFETY`，已有對應錯誤訊息處理
- 圖片格式不在 `{jpg, jpeg, png, gif, webp}` 內會用 PIL 轉 PNG 再送

## 相關

- [[architecture]]
- [[data-flow]]
- [[integration]]
