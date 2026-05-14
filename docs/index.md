---
title: 台灣產業商情平台 — 總覽
status: living
last_updated: 2026-05-13
source_repo: ~/taiwan-company
---

# 台灣產業商情平台

## 一句話

以「公司」為單位、依產業別組織的個人化商情工作台：上傳名單就能自動補齊公開登記資料、抓取每日產業新聞、產出訪談備忘錄與 DOCX/PDF 報告。

## 解決什麼問題

投資 / 商情研究的日常痛點是「**從一張名單到一份能用的公司簡介**」中間有大量手工：查統編、抓股權結構、爬新聞、寫 call memo、整理競業。本平台把這條鏈條串成單一介面：

- **資料採集自動化**：上傳 PDF / Word / Excel / 圖片名單，靠 OCR + AI 抽出公司清單，再去 g0v / GCIS / TWSE / TPEX 補齊登記資料與上市狀態。
- **法人股權追溯**：自動辨識法人董事、串接 `mops_investee` 反查母子公司。
- **每日產業新聞摘要**：以產業別為單位，每天 08:00 自動跑新聞 digest 與本季趨勢。
- **訪談流程閉環**：上傳逐字稿 / 音檔 → Whisper 轉文字 → AI 抽 call memo → 套用範本下載 DOCX。

## 主要功能模組

依 `routers/` 反推：

- **companies**（最大宗）— 列表 / 建立 / 編輯 / 補資料（enrich）/ 深度補資料（deep-enrich）/ 母子公司關係圖 / 專利爬取 / 匯出（DOCX、PDF）
- **upload** — 檔案上傳、OCR、AI 抽公司名稱
- **config** — 產業別、標籤、群組設定；雲端 / 本機部署模式切換
- **call_memo** — 訪談備忘錄 CRUD、逐字稿 / 音檔抽取、DOCX 下載
- **industries** — 每日新聞 digest、本季趨勢

## 適用場景

- 創投 / PE / 投資銀行的案源管理（前期粗篩、追蹤名單）
- 「創業大聯盟」這類比賽 / 配對活動的決賽名單管理（從現有 labels 看得出）
- 個人投資研究者建立小規模的台灣公司資料庫
- 任何要把「一張公司名單」變成「結構化資料 + 可分享報告」的場合

## 相關

- [[architecture]] — 技術架構
- [[data-flow]] — 資料如何進來、如何流動
- [[ai-features]] — AI 在這個平台做什麼
- [[integration]] — 與 mops_investee、GCIS、TWSE 等的串接
- [[glossary]] — 術語表
