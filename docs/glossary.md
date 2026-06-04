---
title: 術語表
status: living
last_updated: 2026-05-13
source_repo: ~/taiwan-company
---

# 術語表

按字首排序，業務與技術詞混排。

## 業務 / 領域

- **產業商情** — 以「公司 + 產業」為單位的市場資訊集合：登記資料、股權結構、新聞動態、競業關係。本平台的核心 domain。
- **Call memo（訪談備忘錄）** — 投資 / 商情訪談後產出的標準化文件，含 ~24 個欄位（受訪人、財務、客戶、風險等）。本平台用 DOCX 範本產出。
- **公司資料** — 統指 `companies.json` 裡一筆 record，含登記資料、AI 產的 summary、董監事、追蹤旗標、call memo。
- **產業別** — 使用者自訂的分類維度，目前實檔有「前瞻科技 / 消費生活 / 綠色永續 / AI / 循環經濟」。前端側欄以產業別組織，每個產業別有獨立的每日 digest。
- **標籤（labels）** — 公司的多選 tag，與產業別正交。實檔範例：「綠色配投」「創業大聯盟決賽 2026」「觀察名單」。常用來標記案源批次或活動。
- **綠色配投** — 「環境部綠色成長基金 加強投資綠色成長淨零產業實施方案」的暱稱，是其中一個案源批次的標籤，平台用來追蹤該批名單的進度。
- **群組（group）** — 公司的單選歸類，比 labels 更結構化（一公司一 group）。
- **配對 / 配投** — 投資配對活動的概稱（如「綠色配投」）。平台本身不做媒合，只做名單管理與分析。
- **Enrich** — 「補資料」動作。基礎 enrich 補登記資料、上市狀態與 AI 簡介；deep-enrich 多跑法人股權、母子公司、專利。
- **法人董事 / 法人代表** — 董事欄 `representative_of` 不為空時，表示這位自然人是某法人股東派駐的董事。是反查母子公司的關鍵線索。
- **公發公司** — 有對 MOPS（公開資訊觀測站）揭露財報的公司（含上市櫃、興櫃、創新板、公開發行）。`listing_status` 欄位的反義是「非公發」。
- **每日 digest** — 每天 08:00 自動產生的「該產業別當日重要新聞摘要」，由 Claude 從 Google News RSS 整理。
- **本季趨勢** — 每天 08:05 跑、約週級更新的「該產業本季重要走向」分析。

## 技術

- **AI 引擎模式（local CLI vs API）** — 本平台支援兩種 AI 呼叫方式：
  - **本機 Claude CLI**：`subprocess` 叫 `claude -p <prompt>`，不消耗 API 費用，自動探尋 PATH / Bun cache / gstack node_modules
  - **雲端 API**：使用者在 UI 輸入 Anthropic / OpenAI / Gemini Key，存 `localStorage`，每次請求帶 `X-API-Key` / `X-AI-Provider` header
  - 預設模型：`claude-sonnet-4-6` / `gpt-4o` / `gemini-2.5-flash`，可用環境變數覆寫。
- **BYOK（Bring Your Own Key）** — 雲端部署模式下，使用者自帶 API Key，伺服器不留 Key（commit f217149 加入）。
- **systemd user service** — Linux 的 user-level systemd unit（`~/.config/systemd/user/`），登入後自動啟動，crash 自動重啟。本平台 unit 名 `taiwan-company.service`。
- **SSE（Server-Sent Events）** — 補資料、深度補資料、關係圖、專利爬取四個長任務都用 SSE 串流回報進度（避免 WebSocket 複雜度，用一條單向 HTTP 就夠）。
- **g0v ronnywang API** — 民間整理的台灣公司登記資料免費 API（`company.g0v.ronny.tw`），補基本登記資料的主來源。
- **GCIS App1 API** — 經濟部商工登記公示資料開放介面，用統編查時可補設立日、最後異動、登記機關。
- **TWSE / TPEX / GISA** — 台灣證交所 / 證櫃買中心 / 創新板 開放資料，用來標 `listing_status`。
- **TIPO** — 經濟部智慧財產局，`patent_scraper.py` 從這裡爬專利。
- **`mops_investee`** — 另一個本機服務（`MOPS_INVESTEE_URL=http://localhost:8080`），反查公發公司財報附註裡揭露持有目標公司股份的母公司清單。詳見 [[integration]]。
- **Whisper（OpenAI）** — 本機跑的語音轉文字模型，僅 local 部署可用（雲端 build 會剝掉以省 ~2 GB PyTorch）。
- **tesseract / pytesseract** — 圖片 OCR，繁中包 `tesseract-ocr-chi-tra` 是必裝。

## 相關

- [[index]]
- [[ai-features]]
- [[integration]]
