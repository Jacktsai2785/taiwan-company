---
title: 外部整合
status: living
last_updated: 2026-05-13
source_repo: ~/taiwan-company
---

# 外部整合

## TL;DR

平台不直連任何 DB，所有外部資料都透過 HTTP API 拿。最重要的整合是 `mops_investee`（同機本地服務）—— 用來反查公發公司股權揭露，補上「誰持有這家公司」的關鍵資訊。

## 與 `~/mops_databases/` 的關係

- **不直連**任何 PostgreSQL / SQLite，符合使用者全域指令「禁止建議直連 PostgreSQL，一律走 API 或 MCP」
- 唯一接觸點是 `services/mops_investee_client.py` → `http://localhost:8080/reverse-lookup/investee`
- `MOPS_INVESTEE_URL` 預設 `http://localhost:8080`、可用 `MOPS_INVESTEE_API_KEY` 帶 `X-API-Key` header
- 用途：給定一家「目標公司」，反查 MOPS 所有公發公司財報附註裡，誰揭露持有這家公司股份。輸出含 `holder_id`（公發母公司統編）、持股比、揭露來源等
- 觸發點：
  - `GET /api/companies/investee-lookup?name=...&tax_id=...&fuzzy=...`（按名稱直接查，不限 DB 內公司）
  - `GET /api/companies/{id}/investee-holders`（DB 內已存公司）
  - 深度 enrich / 母子公司關係圖建構過程也會自動呼叫
- 對應到 `~/mops_databases/mops_investee/`（Jack 自維 7 個 MOPS 子 DB 之一），完整背景見 [`~/mops_databases/docs/mops_investee.md`](~/mops_databases/docs/mops_investee.md)

## g0v ronnywang API

- `https://company.g0v.ronny.tw/api/search` / `/api/fund` / `/api/name`
- 補公司基本登記資料的主力來源（統編、資本、負責人、地址、董監事）
- 沒有 API Key、沒有節流策略 — `gcis_client.py` 用 `httpx.AsyncClient` 直接打，timeout 20 秒
- g0v 並無明文要求節流，目前運作正常

## GCIS App1 API（經濟部商工登記）

- `https://data.gcis.nat.gov.tw/od/data/api/5F64D864-61CB-4D0D-8AD9-492047CC1EA6`
- ronnywang 沒中時的 fallback，用統編查
- 多補：`setup_date` / `last_change_date` / `register_org`

## TWSE / TPEX / GISA 開放資料

用來判定 `listing_status`：

- TWSE 上市：`https://openapi.twse.com.tw/v1/opendata/t187ap03_L`
- TPEX 上櫃：`https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O`
- TPEX 興櫃：`https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_R`
- GISA 創新板：`https://www.tpex.org.tw/openapi/v1/tpex_gisa_company`（無統編、用簡稱對應）

整批快取在 `gcis_client._by_taxid` / `_by_name` / `_by_abbrev`，TTL 24 小時。

> 注意：**創櫃板**目前沒公開 JSON API，無法自動辨識（程式碼註解明示）。

## TIPO 專利

- `services/patent_scraper.py` — 經濟部智慧財產局專利檢索
- 透過 SSE 串流回報進度（`GET /api/companies/{id}/patents`）
- 已知問題（commit 99b5e4e）：TIPO 可能擋雲端 IP，雲端部署時 timeout 會比較頻繁

## Google News RSS

- `services/news_fetcher.py` 用 `feedparser` 抓 Google News
- 過濾中國媒體（`_BLOCKED_SOURCES` 寫死人民日報、新華社、CCTV、觀察者網等）
- 用產業同義詞展開查詢（hardcoded `_INDUSTRY_SYNONYMS` + AI 推薦的 `industry_keywords.json`）

## AI Provider（Anthropic / OpenAI / Gemini）

詳見 [[ai-features]]。預設走本機 `claude` CLI，使用者可在 UI 切到任一 provider。

## 不依賴的東西

- **無資料庫**：不接 PostgreSQL / MySQL / SQLite。所有資料 JSON 落地。
- **無 ORM / 無 migration**：因為沒 DB。
- **無 task queue**：背景任務用 `asyncio.create_task`，狀態存 in-memory dict（`_progress` / `_running`）。重啟會掉所有進行中任務。
- **無認證 / 無多人**：CORS `allow_origins=["*"]`，沒 user model。預設僅供單機 / 單人使用。

## 環境變數總覽（從 `.env.example`）

| 變數 | 用途 | 必填 |
|---|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API Key（雲端部署必填） | 雲端必填 |
| `CLAUDE_CLI_PATH` | 強制指定本機 CLI 路徑 | 否 |
| `CLAUDE_MODEL` | 預設 `claude-sonnet-4-6` | 否 |
| `OPENAI_MODEL` | 預設 `gpt-4o` | 否 |
| `GEMINI_MODEL` | 預設 `gemini-2.5-flash` | 否 |
| `MOPS_INVESTEE_URL` | 預設 `http://localhost:8080` | 否（本機已跑就行） |
| `MOPS_INVESTEE_API_KEY` | 反查服務的 API Key | 視 `mops_investee` 設定 |

## 相關

- [[architecture]]
- [[data-flow]]
- [[ai-features]]
