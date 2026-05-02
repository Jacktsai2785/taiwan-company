# -*- coding: utf-8 -*-
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from playwright.sync_api import sync_playwright
import re

FINDBIZ_URL = 'https://findbiz.nat.gov.tw/fts/query/QueryBar/queryInit.do'
BROWSER_UA = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/120.0.0.0 Safari/537.36'
)

search_term = '地天泰農業生技股份有限公司'

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
    page.keyboard.press('Enter')

    page.wait_for_load_state('networkidle', timeout=20000)
    page.wait_for_timeout(3000)

    body = page.inner_text('body')
    print('=== PAGE BODY AFTER SEARCH ===')
    print(body[:3000])
    print()
    print('=== ALL LINKS ===')
    links = page.query_selector_all('a')
    for link in links:
        href = link.get_attribute('href') or ''
        txt = link.inner_text().strip()
        if txt:
            print(f'  [{txt}] -> {href}')

    browser.close()
