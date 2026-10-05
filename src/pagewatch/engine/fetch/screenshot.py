"""The screenshot method: the browser fetch, plus a PNG of the rendered page.

Fixed 1366×900 viewport, device scale 1, animations and the text caret off, full page or a clipped
rectangle (``fetch.browser.full_page`` / ``clip``). Unlike the browser method, images, media and
fonts are loaded, because they are the thing being compared. The picture is compared against the
previous one by ``pipeline/screenshot.py``.
"""

from __future__ import annotations

from pagewatch.engine.fetch.browser import BrowserFetcher


class ScreenshotFetcher(BrowserFetcher):
    screenshot = True
