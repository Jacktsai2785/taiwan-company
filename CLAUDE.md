# 台灣產業商情平台

FastAPI 後端 + 靜態前端的公司資料分析平台，使用本機 Claude CLI 作為 AI 引擎。

## 快速建立環境

需要安裝：**uv**（Python 套件管理）、**Claude Desktop 或 Claude CLI**

```bash
# 1. 一鍵建立虛擬環境並安裝所有套件
make setup

# 2. 啟動伺服器
make start
```

開啟瀏覽器至 http://localhost:8003

> Port 分配統一見 `~/PORTS.md`。本專案佔 8003（與其他本機服務切開，避開 mops cluster 的 8080–8086）。

## AI 模式說明

- **本機 Claude（預設）**：自動偵測已安裝的 Claude Desktop / `claude` CLI，無需 API Key
- **雲端 API**：點側邊欄 ⚙ 按鈕，輸入 Anthropic / OpenAI / Gemini API Key

## 專案結構

```
main.py              FastAPI 入口
routers/             API 路由（companies, upload, config, call_memo）
services/            業務邏輯（claude_client, gcis_client, data_store 等）
static/              前端（index.html, app.js, style.css）
data/                執行時資料（不在 git 追蹤範圍）
  companies.json     公司資料
  config.json        產業別設定
  call_memo_template.docx  訪談備忘錄範本
```

## 常用指令

```bash
make setup     # 建立環境（第一次或切換裝置後執行）
make start     # 前景啟動（含 hot reload，關 terminal 會掉）
make start-bg  # 背景啟動（與 terminal 解耦，關 VSCode 不會掉）
make stop      # 停止背景 server
make logs      # 追蹤背景 server 的日誌（/tmp/taiwan-company.log）
```

## 對外文件（`docs/`）

本專案的對外知識集中在 `docs/`，給其他 agent 與知識庫引用。改 source 之後，請同步更新對應的 docs 頁。

| 頁 | 內容 |
|---|---|
| `docs/index.md` | 平台一句話 + 模組總覽 |
| `docs/architecture.md` | 後端 / 前端 / 部署架構 |
| `docs/data-flow.md` | 公司資料的生命週期 + companies.json schema |
| `docs/ai-features.md` | AI 用在哪、雙模式邏輯、provider 切換 |
| `docs/integration.md` | 與 mops_investee / GCIS / TWSE 的串接 |
| `docs/glossary.md` | 業務 + 技術術語表 |

**docs 風格**：每頁 200-500 字、frontmatter 統一格式（含 `status` / `last_updated` / `source_repo`）、不用 emoji、不確定的標 `_TODO_`。

## 被 jk_nb 引用

本 repo 的 `docs/` 被 Jack 的個人 wiki（`~/jk_nb/`）以 symlink 方式引用：

```
~/jk_nb/wiki/_external/taiwan-company/ → ~/taiwan-company/docs/
```

實作後果：

- `docs/*.md` 的**檔名**與**內部 anchor** 一旦穩定就不要隨便改（會打破 jk_nb 的 `[[_external/taiwan-company/...]]` 引用）
- 要拆 / 改 docs 結構前，請先 grep 一下 `~/jk_nb/wiki/` 有沒有引到舊檔名
- 對外 docs 是「**living**」狀態——會跟著程式碼演進但保持穩定的篇目骨架

## 設計鐵則 / 不要做的事

寫過幾次差點走偏的方向，明文禁止以下（除非使用者主動要求）：

- **不要加資料庫**。所有資料 JSON 落地。如果效能不夠，先想 indexing / cache，最後才考慮 DB。
- **不要加認證**。本平台預設單人單機，CORS `*`。要加 auth 是大改動，請先討論。
- **不要加前端框架**（React / Vue 之類）。`static/app.js` 是純 JS，刻意保持「打開就能改」。
- **不要在 service 層直接依賴外部 DB**。需要 MOPS 資料時，走 `mops_investee_client` 之類的 HTTP client，符合使用者「禁止直連 PostgreSQL」全域指令。
- **不要把 API Key 寫進 source**。BYOK 流程已經建好，使用者透過 UI 帶 header。
- **不要刪 `companies.json` 的欄位**（或重新命名）。前端 + AI 抽取流程都依賴現有 schema，加欄位 OK，刪欄位需先檢查所有讀取點。

## 常見任務速查

| 任務 | 該改哪 |
|---|---|
| 加新的 AI provider | `services/claude_client.py`（加 `_ask_xxx` + `ask` 分支） |
| 加新的公司資料來源 | `services/gcis_client.py` 或新開一個 client |
| 改 call memo 範本 | `data/call_memo_template.docx`（DOCX 直接改） |
| 加新的 enrich 步驟 | `routers/companies.py` 的 `_enrich_company` / `_deep_enrich_company` |
| 改前端 | `static/app.js`（單檔較大，用 Ctrl+F） |
| 加新產業同義詞 | UI 設定面板 → 走 `industry_keywords.json` 流程；hardcoded 在 `services/news_fetcher.py` `_INDUSTRY_SYNONYMS` |

> 改任何 `*.py` 後，若是 systemd service 在跑，要 `systemctl --user restart taiwan-company` 才會生效（沒有 `--reload`）。詳見下方 systemd 區段。

## systemd user service（開機自啟 + crash 自動重啟）

前後端統一由一個 FastAPI process 服務，已設定為 systemd user service：

```bash
# 查看狀態
make status                            # 或 systemctl --user status taiwan-company

# 啟動 / 停止 / 重啟
systemctl --user start taiwan-company
systemctl --user stop taiwan-company
systemctl --user restart taiwan-company

# 開機自啟 (已預設 enable)
make enable    # 設為開機自啟
make disable   # 取消開機自啟

# 查看 service log
journalctl --user -u taiwan-company -f
# 或查看 append log 檔案
tail -f /home/jacktsai/taiwan-company/logs/app.log
tail -f /home/jacktsai/taiwan-company/logs/app-error.log
```

Service 檔案位置：`~/.config/systemd/user/taiwan-company.service`
- `Restart=always`：crash 後 5 秒自動重啟
- `WantedBy=default.target`：使用者登入後即自動啟動

### ⚠️ 改完 Python 檔後一定要 restart service

systemd service 跑的是 `uvicorn main:app`（**沒有** `--reload`），所以改完任何 `*.py`（`services/`、`routers/`、`main.py`…）都**不會自動套用**，必須手動重啟：

```bash
systemctl --user restart taiwan-company
```

「我明明改好了，為什麼 UI 還是看到舊行為？」八成是這個。對照方式：

- `make start`（前景）→ 有 `--reload`，存檔即生效
- systemd service（背景 / 開機自啟）→ **沒有** `--reload`，必須 restart

純改 `static/`（前端 JS/CSS/HTML）不用 restart，瀏覽器 hard reload (Ctrl+Shift+R) 即可——FastAPI 只是 serve static file。

## 注意事項

- `data/companies.json` 和 `data/config.json` 不在 git 追蹤範圍，每台裝置獨立儲存
- 系統依賴 `tesseract-ocr`（含繁中語言包）處理圖片 OCR，Linux 上需手動安裝：
  `sudo apt install tesseract-ocr tesseract-ocr-chi-tra`
