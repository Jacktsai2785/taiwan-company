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

開啟瀏覽器至 http://localhost:8000

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
make setup    # 建立環境（第一次或切換裝置後執行）
make start    # 啟動開發伺服器（含 hot reload）
```

## 注意事項

- `data/companies.json` 和 `data/config.json` 不在 git 追蹤範圍，每台裝置獨立儲存
- 系統依賴 `tesseract-ocr`（含繁中語言包）處理圖片 OCR，Linux 上需手動安裝：
  `sudo apt install tesseract-ocr tesseract-ocr-chi-tra`
