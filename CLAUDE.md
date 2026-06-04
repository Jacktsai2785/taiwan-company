# 台灣產業商情平台

FastAPI 後端 + 靜態前端的公司資料分析平台，使用本機 Claude CLI 作為 AI 引擎。

## 🤖 給 AI Agent：首次部署協議（最優先，先讀這段）

**情境**：使用者在一台新裝置上裝好 `claude` 後，把這個 repo 的 URL 貼給你、要你 clone。
他明確表示「**除了貼 URL 與裝 claude，其餘步驟都不會自己操作**」。所以只要符合以下任一情境，
就**主動完成整個部署，不要把指令丟回去給他自己跑**：

- 你剛幫他 clone 完這個 repo
- 他只丟 repo URL，或說「裝起來 / 部署 / 跑起來」
- `.venv/` 不存在、或 `curl -fsS http://localhost:8003/` 不通

**動作（依序，不需再問）：**

1. `cd` 到 repo 根目錄，執行： `bash scripts/bootstrap.sh`
   — 冪等腳本，自動裝：系統套件（tesseract 繁中、ffmpeg）、uv、Python 套件、
   playwright Chromium + 系統庫、claude CLI、建 `.env`、設定並啟動 systemd service、healthcheck。
   需 `sudo` 的步驟會自動帶；過程中若 apt / sudo 需要密碼，請提醒使用者輸入。
2. 跑完回報 http://localhost:8003 是否 healthcheck 通過。
3. 提醒使用者**唯一的手動步驟**：本機 AI 引擎需執行一次 `claude` 登入授權；
   或在 `.env` 填 `ANTHROPIC_API_KEY` 改走雲端。
4. 若 healthcheck 失敗：讀 `logs/app-error.log` 或 `journalctl --user -u taiwan-company -n 50`，
   定位後直接修，不要停在「請你自己看 log」。

> 純人類操作（不透過 agent）的等價指令見 [README.md](README.md)。

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

### 批次重生成競業表（維護任務 `taiwan-regen`）

把舊格式公司簡介（legacy 4 欄競業表、多公司同格）批次重生成為新版（5 欄含競業類型、一列一家）的長跑維護工具。腳本 `scripts/regen_summaries.sh`，掛成獨立 systemd user service。

設計重點：

- **自給自足**：每次從 `companies.json` 現況**實算**「還沒新版的」清單（非新版 5 欄、或多公司同格），完成的會自動排除 → 中途停/重啟都只做剩下的、不重做。狀態在資料本身，不依賴 `/tmp`。
- **撞用量上限自動等待**：本機 Claude CLI 是 5 小時滾動窗口、每窗口約 20 間；偵測到上限訊息就每 15 分鐘重試，額度回血自動接續。
- **排除規則**：跳過貼了「潛在案源」標籤、有套用補充資料（`materials_*`）、在 skip-list（連續失敗 3 次）的公司。`PRIORITY_LABELS` / `PRIORITY_ONLY` 可調優先或只跑優先。
- **flock**：同時只允許一個實例（systemd 重啟 / 手動執行不疊跑）。
- **service**：`Restart=on-failure`（crash 5 分鐘後重起）、`WantedBy=default.target`（開機自啟，靠 `Linger=yes`）。跑完 exit 0 不重啟。

```bash
# 控制（範本：scripts/taiwan-regen.service → ~/.config/systemd/user/）
systemctl --user status taiwan-regen
systemctl --user stop taiwan-regen      # 暫停（把額度讓給其他事；勿用 pkill，會被 service 自動重起）
systemctl --user start taiwan-regen     # 續跑
systemctl --user disable taiwan-regen   # 全部跑完後關掉開機自啟
tail -f logs/regen_progress.log         # 即時進度
```

> 改了排除/優先規則（`scripts/regen_summaries.sh` 內的 `EXCLUDE_LABELS` / `PRIORITY_*`）後 `systemctl --user restart taiwan-regen` 生效。

## 注意事項

- `data/companies.json` 和 `data/config.json` 不在 git 追蹤範圍，每台裝置獨立儲存
- 系統依賴 `tesseract-ocr`（含繁中語言包）處理圖片 OCR，Linux 上需手動安裝：
  `sudo apt install tesseract-ocr tesseract-ocr-chi-tra`
