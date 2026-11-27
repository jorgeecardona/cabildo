# ruff: noqa: INP001 (a script the probe runs, not a module of a package)
"""Save a picture of the probe's VS Code window: ``shot.py <debugging port> <png>``."""

import sys

from playwright.sync_api import sync_playwright

port, target = sys.argv[1], sys.argv[2]
with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    pages = [page for context in browser.contexts for page in context.pages]
    window = next(page for page in pages if "workbench" in page.url)
    window.screenshot(path=target)
    print(f"picture: {target}")
