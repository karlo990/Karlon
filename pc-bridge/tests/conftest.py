import glob
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _chromium_path():
    hits = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome"))
    return hits[-1] if hits else None


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        exe = _chromium_path()
        kwargs = {"executable_path": exe, "args": ["--no-sandbox"]} if exe else {}
        b = pw.chromium.launch(**kwargs)
        yield b
        b.close()


@pytest.fixture()
def page(browser):
    p = browser.new_page(viewport={"width": 900, "height": 700})
    yield p
    p.close()
