---
title: 技術架構
status: living
last_updated: 2026-05-13
source_repo: ~/taiwan-company
---

# 技術架構

## TL;DR

單一 FastAPI process 同時供 API 與靜態前端，資料用 JSON 檔存，AI 走「本機 Claude CLI 優先、雲端 API 作備援」的雙模式。Linux 上以 systemd user service 跑，Railway 用 Docker 部署。

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
- AI 設定靠 `localStorage` 存使用者輸入的 API Key，每次請求帶 `X-API-Key` / `X-AI-Provider` header（見 `services/ai_deps.py`）。

## 資料儲存（JSON 檔，無 DB）

`data/` 底下幾隻檔案就是「資料庫」：

- `companies.json` — 公司主資料（5 MB+，數百筆）
- `config.json` — 產業別、標籤
- `industry_keywords.json` — 產業同義詞（AI 擴充用）
- `daily_digest.json` — 每日新聞 digest 快取
- `industry_trends.json` — 本季趨勢快取
- `call_memo_template.docx` — DOCX 範本

**並發策略**：`update_companies_industry` 用 single read-modify-write 規避 race，但整體仍是「全讀全寫」模式，**不適合多人同時寫**。

## AI 整合（雙模式）

詳見 [[ai-features]]。簡述：

- **本機優先**：未設定 API Key 時走 `subprocess` 叫本機 `claude` CLI（自動探尋路徑）。
- **雲端 BYOK**：使用者在 UI 輸入 Anthropic / OpenAI / Gemini Key，存 localStorage、每次請求帶 header。
- 圖片辨識（`extract_companies_from_image`、OCR fallback 之上的 Vision）三家 API 都支援。

## 部署

- **本機開發**：`make start`（前景 + hot reload）/ `make start-bg`（背景 nohup）
- **本機常駐**：`systemd --user` service（`~/.config/systemd/user/taiwan-company.service`），`Restart=always`、`WantedBy=default.target`，登入後自動跑、crash 自動重啟。
- **雲端**：Railway 走 Dockerfile（`python:3.12-slim` + tesseract），雲端 build 會剝掉 `openai-whisper`（PyTorch ~2GB），音檔轉文字僅本機可用。Healthcheck `/health`。

## 相關

- [[index]] — 平台總覽
- [[data-flow]] — 資料如何流動
- [[integration]] — 外部服務串接
