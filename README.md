# 台灣產業商情平台

FastAPI 後端 + 靜態前端的公司資料分析平台，使用本機 Claude CLI 作為 AI 引擎。

---

## 🚀 新裝置一鍵安裝（從這裡開始）

**第一次拿到這個 repo？把下面這一整行複製、貼到終端機（Terminal / WSL），按 Enter 就好：**

```bash
curl -fsSL https://raw.githubusercontent.com/Jacktsai2785/taiwan-company/main/scripts/install.sh | bash
```

它會：裝 git → clone 到 `~/taiwan-company` → 跑 `scripts/bootstrap.sh` 安裝完整環境
（系統套件、uv、Python 套件、playwright Chromium + 系統庫、claude CLI、systemd service）→ healthcheck。

腳本**冪等**，可重複執行。已 clone 過的裝置改用：`make bootstrap`。

完成後開啟 http://localhost:8003

### 唯一的手動步驟

本機 AI 引擎需登入一次（無法自動化）。預設引擎為 claude：

```bash
claude          # 完成 OAuth 授權
```

要改用其他引擎，可在 `.env` 設 `AI_ENGINE`（`claude` / `codex` / `gemini` / `ollama`），
或在側邊欄 ⚙ 臨時切換。各引擎需各自先安裝並登入對應 CLI（codex / gemini）或啟動 Ollama 服務。

---

## 🤖 如果你是 AI Agent（Claude Code）正在讀這份 repo

使用者的部署習慣是：**在新裝置裝好 `claude` → 把本 repo URL 貼給你 → 其餘不自己操作**。

若你剛 clone 完、或使用者要你「部署 / 裝起來」，請**直接執行**並全程自動完成：

```bash
bash scripts/bootstrap.sh
```

完整協議（觸發條件、回報內容、失敗處理）見 [`CLAUDE.md`](CLAUDE.md) 最上方的「首次部署協議」。

---

## 常用指令

| 指令 | 用途 |
|---|---|
| `make bootstrap` | 一鍵安裝完整環境（新裝置首次） |
| `make setup` | 只重建 Python 環境（系統依賴已就緒） |
| `make start` | 前景啟動（hot reload） |
| `make start-bg` | systemd 背景啟動 |
| `make stop` | 停止背景 server |
| `make restart` | 套用新 code（`git pull` 後執行） |
| `make logs` | 追蹤日誌 |
| `make status` | 查看 service 狀態 |

更多細節（架構、systemd、設計鐵則、常見任務速查）見 [`CLAUDE.md`](CLAUDE.md)。
