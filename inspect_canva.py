from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


def is_canva_page(page):
    hostname = (urlparse(page.url).hostname or "").lower()

    return (
        hostname == "canva.com"
        or hostname.endswith(".canva.com")
    )


with sync_playwright() as playwright:
    browser = playwright.chromium.connect_over_cdp(
        "http://127.0.0.1:9222"
    )

    if not browser.contexts:
        raise RuntimeError("Không tìm thấy Chrome context")

    context = browser.contexts[0]

    canva_pages = [
        page
        for page in context.pages
        if is_canva_page(page)
    ]

    if canva_pages:
        page = canva_pages[-1]
    else:
        page = context.new_page()
        page.goto(
            "https://www.canva.com/",
            wait_until="domcontentloaded",
        )

    page.bring_to_front()

    print(f"[CONNECTED] Title: {page.title()}")
    print(f"[CONNECTED] URL: {page.url}")
    print("[INSPECTOR] Đang mở Playwright Inspector...")

    page.pause()