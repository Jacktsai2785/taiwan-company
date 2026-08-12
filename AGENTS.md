# AI Agent 專案規則

本檔是 Codex 與其他會讀取 `AGENTS.md` 的開發 Agent 在本專案中的持久工作指示。

## Release 與 commit + push 流程

版本的唯一來源是根目錄的 `VERSION`，版本紀錄的唯一來源是 `CHANGELOG.md`。不得只修改其中一個。

當使用者明確要求「commit + push」、要求發布版本，或目前工作已形成一批可交付的產品變更時，依序執行：

1. 檢查 Git working tree，區分本次變更與使用者原有、無關的未提交變更；不得擅自納入無關檔案。
2. 檢查自 `VERSION` 所代表版本以來尚未發布的變更，依 Semantic Versioning 判斷升版幅度：
   - `PATCH`：修正錯誤、相容性或內部可靠性改善，沒有新增使用者功能。
   - `MINOR`：新增向下相容、使用者可感知的功能或工作流程。
   - `MAJOR`：造成不相容的資料格式、API、操作流程或部署方式變更；不確定時先詢問使用者。
3. 使用 `scripts/release.py` 同步更新 `VERSION` 與 `CHANGELOG.md`，例如：

   ```bash
   .venv/bin/python scripts/release.py patch --fixed "修正內容"
   .venv/bin/python scripts/release.py minor --added "新增功能" --changed "調整內容"
   ```

4. 檢查產生的版本號與 Changelog。Changelog 應描述使用者能理解的結果，不要堆砌 commit 訊息或實作細節。
5. 執行與本次變更相稱的測試、語法檢查及 `git diff --check`；Python 服務有變更時重啟 `taiwan-company` 並執行 healthcheck。
6. 僅 stage 本次範圍內的檔案，建立 commit，然後 push；不得用 `git add .` 混入無關變更。
7. 回報新版本、commit hash、push 結果、測試結果，以及任何未納入的既有 working-tree 變更。

不要因每一筆 commit、push 或微小文字修改機械式地增加 `PATCH`。只有形成可交付版本時才升版。以下情況預設不升版：純開發文件修字、測試本身調整、尚未完成的 WIP commit，以及只修正版本紀錄本身；若使用者明確要求發布則仍依其要求處理。

執行 release 前若目前 `CHANGELOG.md` 已有相同版本的未提交 release 項目，不得再次升版；應沿用並補齊該版本，避免同一批變更重複計算。
