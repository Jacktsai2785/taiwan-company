# 版本更新紀錄

本檔案記錄台灣產業商情平台有意義的版本更新。版本號同步顯示在前端側邊欄左下角，
點擊即可看到這份紀錄（後端讀 `VERSION` 檔、前端讀本檔案）。

## [1.3.1] - 2026-09-18

### Fixed

- 公司官網查詢改用快速搜尋比對，準確度與速度提升，AI 搜尋僅在快速搜尋找不到時才會啟用
- 修正工商登記查詢（findbiz）Cloudflare 驗證誤判成功的問題，避免查詢在驗證後立即失敗
## [1.3.0] - 2026-08-12

### Added

- Call Memo 支援同一間公司建立多份訪談記錄，可用頁籤新增／切換／刪除
- Call Memo 新增「公司技術說明」欄位（比照其他欄位自動抽取）與可收合的「Memo」自由備註欄位

### Changed

- Call Memo 下載檔名改為「公司名稱callmemo_訪談日期.docx」，下載按鈕文字改為「callmemo.docx」
- 「用 AI 更新公司簡介」在有多份 Call Memo 時會一併納入全部訪談內容

## [1.2.0] - 2026-08-10

### Added

- 新增結構化版本紀錄介面，可依新增、調整與修正類型篩選歷史更新
- 新增 `scripts/release.py`，以單一指令同步升級語意化版本與建立版本紀錄
- 產業地圖新增循環流程圖、子產業公司分布與可追溯的公司競爭資料來源

### Changed

- 調整 AI 公司分類與子產業生成規則，改以公司實際業務證據判斷並降低錯誤歸類
- 改善產業地圖與公司 Modal 的開啟順序、視覺層級及桌面版閱讀體驗
- 統一公司 Modal 的競爭分析格式與 Markdown 來源連結呈現
- 更新版本紀錄視窗，支援依新增、調整與修正類型檢視
- 將平台預設收斂為僅本機使用，服務只監聽 localhost

### Fixed

- 修正已生成公司在產業地圖仍被判定為未生成，以及公司 Modal 被地圖遮住的問題
- 修正重複公司合併可能留下半套資料的問題，失敗時會還原 JSON 與上傳檔案
- 修正不同生成批次導致公司 Modal 樣式及競爭分析來源格式不一致的問題
- 修正一般編輯公司資料會取消追蹤狀態、產業地圖並行儲存遺失更新，以及標籤／產業名稱的腳本注入風險
- 修正附件、逐字稿與稽核證據在備份或刪除公司時遺漏的問題；多檔上傳失敗不再留下未記錄檔案
- 修正文字與 CSV 上傳解析、首頁載入失敗提示、批次重生成自動啟動及完整測試可能卡住的問題

## [1.1.0] - 2026-07-28

### Added

- 前端側邊欄底部顯示目前版本號，點擊可開啟本更新紀錄
- 後端新增 `/api/version` 端點與 `VERSION` 檔案，之後每次有意義的更新都會在本檔留紀錄

## [1.0.0] - 2026-07-28

### Fixed

架構卡點修正（跟 Codex 用 collab-review 雙盲查核現行程式碼，確認並修復 6 項）：

- `findbiz_scraper.py` 的 envelope bug（`AttributeError` 導致 `--update-missing` 完全壞掉），
  改走 `data_store` 的原子寫 + 鎖
- `enrichment.py` / `companies.py` 共 6 組零散的 progress/running pattern，收斂成
  `services/task_progress.ProgressChannel`
- `main.py` 兩個獨立 daily scheduler 合併成一條 dependency-aware pipeline；
  `daily_digest.py` 的 `refresh_all_*` 改回傳失敗清單，支援有界重試
- summarize / deep-enrich 失敗時仍誤送 `done`、前端誤標記成功 —— 四個 worker 的
  `done` 事件統一帶 `ok` 欄位；前端新增 `app-core.js` 的 `subscribeSSE()` helper
- `enrichment.py` 新增公開的 `start_enrichment()`，取代其他模組直接 import 私有函式
- `claude_client.classify_ai_error()` 分類額度上限例外，`enrichment.py` 的 error 事件帶 `code`

> 1.0.0 之前沒有正式版本紀錄；更早的開發歷程見 `git log`。
