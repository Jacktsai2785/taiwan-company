---
title: 技術架構
status: living
last_updated: 2026-06-05
source_repo: ~/taiwan-company
---

# 技術架構

## TL;DR

單一 FastAPI process 同時供 API 與靜態前端，資料用 JSON 檔存，AI 走純本機多引擎（claude / codex / gemini CLI、ollama 端點），不需 API Key。地端部署，Linux 上以 systemd user service 跑。

## 後端（FastAPI）

- 入口：`main.py`，以 `uvicorn main:app` 啟動，預設 port `8003`（見 `~/PORTS.md`）。
- 路由分層在 `routers/`：`companies` / `upload` / `config` / `call_memo` / `industries`。每個 router 帶自己的 `prefix`（多半是 `/api/...`）。
- 業務邏輯集中在 `services/`：
  - `claude_client.py` — 統一 AI 呼叫入口（Anthropic / OpenAI / Gemini / 本機 CLI）
  - `data_store.py` — JSON 讀寫（atomic：每次操作都是一次 read + 一次 write）
  - `gcis_client.py` — g0v ronnywang + GCIS App1 + TWSE/TPEX 上市狀態查詢
  - `mops_investee_client.py` — 串接外部 `mops_investee` 服務反查公發母公司
  - `daily_digest.py` — 每日新聞與產業趨勢生成、快取、排程
  - `news_fetcher.py` — Google News RSS 抓取、產業同義詞擴展、過濾中國媒體
  - `company_extractor.py` / `memo_extractor.py` / `company_exporter.py` — AI 抽取與報告產出
  - `file_parser.py` — PDF / DOCX / XLSX / 圖片 文字抽取（搭配 tesseract）
  - `whisper_transcriber.py` — 本機 Whisper 音檔轉逐字稿
  - `patent_scraper.py` — TIPO 專利爬蟲
- Lifespan hook 啟動兩個背景排程：每天 08:00 跑 digest，08:05 跑 trends。
- CORS：`allow_origins=["*"]`（個人工具，未對外鎖定）。

## 前端（純靜態，無框架）

- `static/index.html` + `static/app.js` + `static/style.css`，由 `main.py` 的 `/static` 與 `/` 直接服務。
- **無前端框架**（沒有 React / Vue），原生 JS。`app.js` 約 131 KB，所有互動都在裡面。
- 唯一 CDN 依賴：`cytoscape@3.30.2`（畫母子公司關係圖）。
- AI 引擎選擇靠 `localStorage.ai_engine`，每次請求帶 `X-AI-Engine` header（SSE 走 `?engine=`，見 `services/ai_deps.py`）。無 API Key。

## 資料儲存（JSON 檔，無 DB）

`data/` 底下幾隻檔案就是「資料庫」：

- `companies.json` — 公司主資料（5 MB+，數百筆）
- `config.json` — 產業別、標籤
- `industry_keywords.json` — 產業同義詞（AI 擴充用）
- `daily_digest.json` — 每日新聞 digest 快取
- `industry_trends.json` — 本季趨勢快取
- `call_memo_template.docx` — DOCX 範本

**並發策略**：`update_companies_industry` 用 single read-modify-write 規避 race，但整體仍是「全讀全寫」模式，**不適合多人同時寫**。

## AI 整合（純本機多引擎）

詳見 [[ai-features]]。簡述：

- **四個引擎**：`claude`（預設，本機 CLI）/ `codex`（`codex exec`）/ `gemini`（`gemini -p`）/ `ollama`（本機端點），由 `engine` 字串選擇，全部 `subprocess` 或本機 HTTP，不打雲端 API。
- **無 API Key**：引擎只需各自先安裝並登入對應 CLI，或啟動 ollama 服務。
- 圖片辨識（`extract_companies_from_image`）各引擎用自己的多模態能力；能力不足時退到本機 OCR + 文字抽取。

## 部署

- **本機開發**：`make start`（前景 + hot reload）/ `make start-bg`（背景 nohup）
- **本機常駐**：`systemd --user` service（`~/.config/systemd/user/taiwan-company.service`），`Restart=always`、`WantedBy=default.target`，登入後自動跑、crash 自動重啟。
- **Healthcheck**：`GET /health` 回 `{"status": "ok"}`，bootstrap / `make` 用它確認服務起來。

> 本專案為地端部署，不含雲端 hosting 設定。

## 相關

- [[index]] — 平台總覽
- [[data-flow]] — 資料如何流動
- [[integration]] — 外部服務串接
