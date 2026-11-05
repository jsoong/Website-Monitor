"""Link discovery for Follow-Links (spec: Multi-page sources). Runs in a worker (it parses)."""

from __future__ import annotations

from urllib.parse import urldefrag, urljoin

from pagewatch.engine.pipeline.extract import decode_body, normalize_text, parse_html

MAX_LINKS = 5000
TEXT_CHARS = 120
_SKIP = ("#", "javascript:", "mailto:", "tel:", "data:", "sms:")


def extract_links(body: bytes, content_type: str, base_url: str) -> list[tuple[str, str]]:
    """``(absolute URL, link text)`` for every http(s) link of the page, in document order, each
    URL once (fragments dropped). A page that says ``<meta name="robots" content="nofollow">``
    has none: its owner asked crawlers not to follow it."""
    root = parse_html(decode_body(body, content_type))
    if root is None:
        return []
    for meta in root.iter("meta"):
        if (meta.get("name") or "").lower() in ("robots", "pagewatch") and "nofollow" in (
            meta.get("content") or ""
        ).lower():
            return []
    base = base_url
    base_tag = next(iter(root.iter("base")), None)
    if base_tag is not None and base_tag.get("href"):
        base = urljoin(base_url, base_tag.get("href").strip())
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for a in root.iter("a"):
        href = (a.get("href") or "").strip()
        if not href or href.lower().startswith(_SKIP):
            continue
        try:
            url = urldefrag(urljoin(base, href))[0]
        except ValueError:
            continue
        if not url.lower().startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        text = normalize_text("".join(a.itertext()))
        if not text:
            img = next(iter(a.iter("img")), None)
            text = normalize_text(
                (a.get("title") or (img.get("alt") if img is not None else "")) or ""
            )
        out.append((url, text[:TEXT_CHARS]))
        if len(out) >= MAX_LINKS:
            break
    return out
