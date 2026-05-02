from playwright.sync_api import sync_playwright
import re

FINDBIZ_URL = 'https://findbiz.nat.gov.tw/fts/query/QueryBar/queryInit.do'
BROWSER_UA = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/120.0.0.0 Safari/537.36'
)

TABS = ['董監事資料', '經理人資料', '工廠資料', '歷史資料']

def lookup_company(search_term):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=BROWSER_UA,
            viewport={'width': 1280, 'height': 900},
            locale='zh-TW',
        )
        page = ctx.new_page()
        page.goto(FINDBIZ_URL, wait_until='networkidle', timeout=30000)
        page.wait_for_selector('input[name="qryCond"]', timeout=10000)
        page.fill('input[name="qryCond"]', search_term)
        page.click('input[type="submit"], button:has-text("查詢")')
        page.wait_for_selector('text=/共.*筆/, text=詳細資料, text=查無資料', timeout=15000)
        body = page.inner_text('body')
        match = re.search(r'共\s*(\d+)\s*筆', body)
        if not match or match.group(1) == '0':
            browser.close()
            print('RESULT: No results found')
            return
        result_count = int(match.group(1))
        if result_count > 1:
            browser.close()
            print(f'RESULT: Multiple results ({result_count})')
            print(body[:3000])
            return
        page.click('text=詳細資料')
        page.wait_for_selector('text=統一編號', timeout=15000)
        results = {}
        results['basic'] = page.inner_text('body')
        for tab in TABS:
            try:
                page.click(f'text={tab}')
                page.wait_for_selector('table, text=/無.*資料/, text=/序號/', timeout=10000)
                page.wait_for_timeout(500)
                results[tab] = page.inner_text('body')
            except Exception as e:
                results[tab] = f'Error: {e}'
        browser.close()
        for key, val in results.items():
            print(f'=== {key} ===')
            print(val[:3000])
            print()

lookup_company('高翔永續科技股份有限公司')
