"""Extract PDF links: watch only the list of PDF files a page links to.

The page is replaced, before comparison, by one line per link to a ``.pdf`` file (its text and
its address), so a new report, or a file swapped for another, is a change and nothing else on the
page is. It uses the ``preprocess`` hook. Assign it to a bookmark; there is nothing to configure.
"""

from html import escape
from urllib.parse import urljoin, urlsplit


def preprocess(ctx, html):
    root = ctx.helpers.parse_html(html)
    if root is None:
        return html
    seen = set()
    items = []
    for link in ctx.helpers.select(root, "a[href]"):
        url = urljoin(ctx.bookmark.url, link.get("href").strip())
        if not urlsplit(url).path.lower().endswith(".pdf") or url in seen:
            continue
        seen.add(url)
        label = ctx.helpers.text_of(link) or urlsplit(url).path.rsplit("/", 1)[-1]
        items.append(f'<li><a href="{escape(url)}">{escape(label)}</a> {escape(url)}</li>')
    return "<html><body><ul>" + "".join(items) + "</ul></body></html>"
