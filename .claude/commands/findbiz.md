# /findbiz — 從 findbiz.nat.gov.tw 抓取每股金額

抓取指定公司（或所有缺少每股金額的公司）的「每股金額(元)」與「已發行股份總數(股)」，並更新 `companies.json`。

## 使用方式

```
/findbiz                    # 列出所有缺資料的公司
/findbiz 60737697           # 抓取指定統一編號
/findbiz 永環材料            # 用公司名稱搜尋並抓取
```

## 執行邏輯（依序進行，缺一不可）

### Step 1：確認觸發條件
只有同時滿足以下條件的公司才需要抓取：
- `is_corp == True`（股份有限公司）
- `tax_id` 有值
- `par_value` 為空 / 0
- `no_par_value != True`（非無票面金額公司）

### Step 2：啟動 Persistent Browser Context
```python
ctx = await pw.chromium.launch_persistent_context(
    "data/findbiz_profile/",   # 儲存 cf_clearance cookie
    headless=False,
    args=["--disable-blink-features=AutomationControlled"],
)
```
- 第一次執行：瀏覽器跳出，使用者在 findbiz 頁面手動點「驗證您是真人」
- 之後執行：`cf_clearance` cookie 已存在 profile，自動略過 Cloudflare
- **判斷通過標準**：`ctx.cookies("https://findbiz.nat.gov.tw")` 中有 `cf_clearance`，**不靠 title 或 body 長度**

### Step 3：搜尋公司（POST + expect_navigation）
```python
async with page.expect_navigation(wait_until="domcontentloaded", timeout=30000):
    await page.evaluate("""([url, params]) => {
        const form = document.createElement('form');
        form.method = 'POST'; form.action = url;
        /* 填入 params 的 hidden inputs */
        form.submit();
    }""", [FINDBIZ_LIST, params])
```
關鍵 POST 參數：`qryCond=<tax_id>`, `infoType=D`, `qryType=cmpyType`, `cmpyType=true`, `isAlive=all`

**禁止**使用 `page.request.post()` 或 JS `fetch()`：Cloudflare 會對非瀏覽器導航的 HTTP 請求二次攔截。

### Step 4：點擊公司連結（不能直接 goto detail URL）
```python
link = page.locator("a.hover[href*='queryCmpyDetail']").first
async with page.expect_navigation(wait_until="domcontentloaded", timeout=20000):
    await link.click()
```
- findbiz 詳細頁需透過 JS 攔截 click 後 POST `detailForm`（含 `disj` session token）
- 直接 `page.goto(detail_url)` → 伺服器回「很抱歉，您所輸入的需求有誤」

### Step 5：等待 JS 渲染後讀取
```python
await page.wait_for_selector("#tabCmpyContent tr", timeout=15000)
detail_html = await page.content()
```
詳細頁的 table 由 JavaScript 動態填入，`domcontentloaded` 後立即讀是空殼。

### Step 6：解析並寫入
```python
raw = _parse_detail_html(detail_html)   # 解析 #tabCmpyContent key-value
par_value    = _parse_int(raw.get("每股金額(元)", ""))
total_shares = _parse_int(raw.get("已發行股份總數(股)", ""))
capital      = _parse_int(raw.get("實收資本額(元)", ""))
```
若 `par_value == 0 and total_shares == 0`：公司可能太新尚未完成股份登記，報錯不寫入。

## 常見錯誤與對應處理

| 錯誤訊息 | 原因 | 處理 |
|---------|------|------|
| `Just a moment` in HTML | `cf_clearance` 未設定或失效 | `ctx.clear_cookies()` 後讓使用者重新驗證 |
| `很抱歉，您所輸入的需求有誤` | detail URL 用 GET 存取 | 改用 `link.click()`（Step 4） |
| `#tabCmpyContent tr` timeout | JS 未渲染完成 | 增加等待時間；或確認 detail 頁是否正確載入 |
| `networkidle` timeout | findbiz 背景請求永不停止 | 改用 `domcontentloaded` |
| 搜尋無結果 | 新公司尚未收錄 findbiz | 回報「查無資料」，不視為錯誤 |

## 相關檔案
- 後端實作：`routers/findbiz.py`
- Browser profile：`data/findbiz_profile/`（不進 git）
- 觸發前端：`static/app.js` `fetchParValue()` 函式
- DISPLAY 設定：`~/.config/systemd/user/taiwan-company.service` `Environment=DISPLAY=:0`