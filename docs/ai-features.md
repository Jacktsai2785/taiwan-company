---
title: AI 功能清單
status: living
last_updated: 2026-06-05
source_repo: ~/taiwan-company
---

# AI 功能清單

## TL;DR

平台幾乎每個寫入動作背後都有 AI：抽取公司名、分產業、產簡介、寫摘要、抽 call memo 欄位、整理新聞、推趨勢。AI 呼叫統一走 `services/claude_client.ask()`，由 `engine` 字串決定打哪個**本機引擎**。全部地端執行，**不需 API Key**。

## 核心統一入口

`services/claude_client.py` 提供三個公開函式，皆以 `engine` 選引擎：

- `ask(prompt, timeout, allowed_tools, engine, max_turns, model)` — 文字 prompt
- `ask_with_image(prompt, image_content, suffix, timeout, engine)` — 單張圖多模態（Vision）
- `ask_with_files(prompt, file_paths, timeout, engine, model)` — 多檔多模態（PDF + 多張圖）

四個引擎（`KNOWN_ENGINES`）：

1. `claude`（預設）— 本機 `claude` CLI；多模態用 `--add-dir` + Read tool 原生讀取
2. `codex` — OpenAI 官方 `codex exec` CLI；圖片走 `--image`
3. `gemini` — Google 官方 `gemini -p` CLI；檔案/圖片走 prompt 內 `@路徑`
4. `ollama` — 本機 OpenAI 相容端點（`OLLAMA_BASE_URL`，預設 `localhost:11434`）；圖片需 `OLLAMA_VISION_MODEL`

引擎來源：請求帶 `X-AI-Engine` header / `?engine=`（見 `services/ai_deps.py`），沒帶則用 env `AI_ENGINE`（預設 `claude`）。引擎不認得或對應 CLI/服務未就緒時回可行動錯誤訊息。

**多模態退路**：能力不足時（ollama 未設 vision model 的圖片、codex/ollama 的 PDF）自動退到 `_ask_with_local_extraction`（`file_parser` 抽文字 + tesseract OCR），再以選定引擎做純文字補完，行為可預期。

預設模型透過環境變數覆寫：`CLAUDE_MODEL` / `CODEX_MODEL` / `GEMINI_MODEL` / `OLLAMA_MODEL`。`allowed_tools` 含 `WebSearch` / `WebFetch` 時，claude CLI 透過 `--allowedTools` 啟用對應工具。

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
- 點「✦ 用 Opus 更新公司簡介」後（前端 spinner + 計時動畫），後端把 PDF/圖片交給 `ask_with_files`、office/txt 抽文字內嵌、訪談備忘錄由 `serialize_memo` 組成訪談文字，一起用最新 Opus（`_DEEP_MODEL = "opus"`，CLI 別名自動跟最新）讀過，生成整合版簡介，存 `materials_summary` / `materials_blurb`
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

## 引擎選擇邏輯

- 預設引擎由 env `AI_ENGINE` 決定（預設 `claude`）
- UI 右上角 ⚙ 開設定面板，使用者選引擎（claude / codex / gemini / ollama），存 `localStorage.ai_engine`
- 每次 API 請求由 `ai_from_headers` 從 `X-AI-Engine` header 取出（SSE / EventSource 走 `?engine=` query），傳進 router → service
- 引擎對應的 CLI 未安裝 / Ollama 未啟動時，回可行動的中文錯誤訊息（提示安裝/登入或改選其他引擎）

## 本機 CLI 探尋路徑（`_find_cli`）

依序找：

1. `CLAUDE_CLI_PATH` env
2. `shutil.which("claude")` / `claude.exe`
3. Bun cache：`~/.bun/install/cache/@anthropic-ai/claude-{agent-sdk,code}-win32-x64/<version>/claude.exe`
4. gstack skills：`~/.claude/skills/gstack/node_modules/**/claude{,.exe}`

支援 Windows / Linux / macOS 的混合環境。

## 已知限制

- CLI 引擎單次呼叫有 timeout（文字預設 120 秒、多檔 300 秒），長 prompt 易超時；逾時以 `killpg` 整組終止子 process
- codex / ollama 對 PDF 無原生支援，走本機文字抽取退路
- ollama 處理圖片需設定 `OLLAMA_VISION_MODEL`（如 `llava`），否則退到本機 OCR

## 相關

- [[architecture]]
- [[data-flow]]
- [[integration]]
