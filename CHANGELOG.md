# 版本更新紀錄

本檔案記錄台灣產業商情平台有意義的版本更新。版本號同步顯示在前端側邊欄左下角，
點擊即可看到這份紀錄（後端讀 `VERSION` 檔、前端讀本檔案）。

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
