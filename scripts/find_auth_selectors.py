"""
scripts/find_auth_selectors.py

One-off debug script: prints every button/input[type=submit]-like element
found on the login and signup pages, so we can pick the real selector
instead of guessing. Not part of the test suite — run it manually, read
the output, then update tests/mobile/mobile_targets.py and delete/ignore
this file.

Usage:
    python scripts/find_auth_selectors.py
"""
from playwright.sync_api import sync_playwright

URLS = {
    "login": "https://accounts.appypie.com/login",
    "signup": "https://accounts.appypie.com/register",
}

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    for name, url in URLS.items():
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_timeout(2000)  # let any client-side rendering settle

        print(f"\n{'='*60}\n{name.upper()} — {url}\n{'='*60}")
        elements = page.evaluate(
            """
            () => {
              const els = Array.from(document.querySelectorAll('button, input[type=submit], input[type=button], [role="button"]'));
              return els.map(el => ({
                tag: el.tagName,
                type: el.getAttribute('type'),
                id: el.id || null,
                cls: el.className ? el.className.toString() : null,
                testid: el.getAttribute('data-testid'),
                text: (el.innerText || el.value || '').trim().slice(0, 40),
              }));
            }
            """
        )
        for el in elements:
            print(el)
        page.close()
    browser.close()