"""Save a page's images and stylesheets beside an exported HTML file (``html_assets``).

This is the "partial" Local Website Archive of the spec's appendix: ``<img src>`` and
``<link rel=stylesheet href>`` are downloaded and the document is rewritten to point at the saved
copies. (The viewer renders only a page's body, so the stylesheets named in the original page's
head are passed in and added to the document first.) Things inside stylesheets (``url(...)``
fonts and backgrounds) stay remote. A resource that cannot be fetched is skipped and reported; it
never fails the export.

Limits: 100 resources, 5 MB each, 25 MB in all. A page on the public internet cannot make this
reach into the local network (literal private/loopback addresses and ``localhost`` are skipped
unless the page itself is local).
"""

from __future__ import annotations

import asyncio
import ipaddress
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from lxml import html as lxml_html

from pagewatch.engine.actions.common import write_atomic
from pagewatch.engine.actions.templating import safe_filename
from pagewatch.engine.pipeline.extract import decode_body, parse_html

MAX_ASSETS = 100
MAX_ASSET_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 25 * 1024 * 1024
TOO_BIG = f"larger than {MAX_ASSET_BYTES // 1024 // 1024} MB"
CONCURRENCY = 6
TIMEOUT_S = 15.0


@dataclass(slots=True)
class AssetReport:
    saved: dict[str, str] = field(default_factory=dict)  # absolute URL -> saved file name
    skipped: list[str] = field(default_factory=list)


def _private_host(host: str) -> bool:
    if host.lower() in ("localhost", ""):
        return True
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def _references(
    doc: lxml_html.HtmlElement, base: str
) -> list[tuple[lxml_html.HtmlElement, str, str]]:
    """``(element, attribute, absolute URL)`` for every resource worth saving."""
    out: list[tuple[lxml_html.HtmlElement, str, str]] = []
    for el in doc.iter("img", "link"):
        if el.tag == "link":
            rel = (el.get("rel") or "").lower().split()
            attr = "href"
            if "stylesheet" not in rel:
                continue
        else:
            attr = "src"
        ref = (el.get(attr) or "").strip()
        if not ref or ref.startswith(("data:", "#")):
            continue
        absolute = urljoin(base, ref)
        if urlsplit(absolute).scheme in ("http", "https"):
            out.append((el, attr, absolute))
    return out


def _name_for(index: int, url: str, content_type: str) -> str:
    stem = safe_filename(Path(urlsplit(url).path).name or "asset")
    if "." not in stem:
        ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ""
        stem += ext
    return f"{index:03d}-{stem}"


async def _download(
    client: httpx.AsyncClient, url: str, sem: asyncio.Semaphore
) -> tuple[bytes, str] | str:
    """The body and content type, or a short reason it was skipped."""
    async with sem:
        try:
            async with client.stream("GET", url) as response:
                if response.status_code != 200:
                    return f"{urlsplit(url).netloc}: HTTP {response.status_code}"
                size = int(response.headers.get("content-length") or 0)
                if size > MAX_ASSET_BYTES:
                    return (
                        f"{urlsplit(url).netloc}: larger than {MAX_ASSET_BYTES // 1024 // 1024} MB"
                    )
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_ASSET_BYTES:
                        return f"{urlsplit(url).netloc}: {TOO_BIG}"
                    chunks.append(chunk)
                return b"".join(chunks), response.headers.get("content-type", "")
        except httpx.HTTPError as exc:
            return f"{urlsplit(url).netloc}: {type(exc).__name__}"


MAX_STYLESHEETS = 20


def stylesheet_links(raw: bytes, content_type: str, base_url: str) -> list[str]:
    """Absolute URLs of the stylesheets an HTML page links to (empty for anything else)."""
    root = parse_html(decode_body(raw, content_type))
    if root is None:
        return []
    out: list[str] = []
    for link in root.iter("link"):
        rel = (link.get("rel") or "").lower().split()
        href = (link.get("href") or "").strip()
        if "stylesheet" in rel and href:
            absolute = urljoin(base_url, href)
            if urlsplit(absolute).scheme in ("http", "https") and absolute not in out:
                out.append(absolute)
    return out[:MAX_STYLESHEETS]


def _add_stylesheets(doc: lxml_html.HtmlElement, urls: list[str]) -> None:
    head = doc.find("head")
    if head is None:
        head = doc.makeelement("head")
        doc.insert(0, head)
    for url in urls:
        link = head.makeelement("link", {"rel": "stylesheet", "href": url})
        head.append(link)


async def localize_assets(
    html: str,
    page_url: str,
    folder: Path,
    folder_name: str,
    stylesheets: list[str] | None = None,
) -> tuple[str, AssetReport]:
    """Download the page's images and stylesheets into ``folder`` and return the document with
    its references rewritten to ``folder_name/<file>``."""
    report = AssetReport()
    doc = await asyncio.to_thread(lxml_html.document_fromstring, html)
    if stylesheets:
        await asyncio.to_thread(_add_stylesheets, doc, stylesheets)
    refs = _references(doc, page_url)
    page_host = urlsplit(page_url).hostname or ""
    page_is_local = _private_host(page_host)

    wanted: list[str] = []
    for _, _, url in refs:
        if url in wanted:
            continue
        host = urlsplit(url).hostname or ""
        if _private_host(host) and not page_is_local and host != page_host:
            report.skipped.append(f"{host}: a local address, not fetched for a public page")
        elif len(wanted) >= MAX_ASSETS:
            report.skipped.append(f"{host}: more than {MAX_ASSETS} resources")
        else:
            wanted.append(url)

    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(
        timeout=TIMEOUT_S, follow_redirects=True, trust_env=False,
        headers={"User-Agent": "PageWatch export"},
    ) as client:  # fmt: skip
        results = await asyncio.gather(*(_download(client, u, sem) for u in wanted))

    total = 0
    for index, (url, got) in enumerate(zip(wanted, results, strict=True), start=1):
        if isinstance(got, str):
            report.skipped.append(got)
            continue
        body, ctype = got
        total += len(body)
        if total > MAX_TOTAL_BYTES:
            report.skipped.append(
                f"{urlsplit(url).netloc}: over the {MAX_TOTAL_BYTES // 1024 // 1024} MB total"
            )
            continue
        name = _name_for(index, url, ctype)
        await asyncio.to_thread(write_atomic, folder / name, body)
        report.saved[url] = name

    def rewrite() -> str:
        for el, attr, url in refs:
            saved = report.saved.get(url)
            if saved is not None:
                el.set(attr, f"{folder_name}/{saved}")
            elif el.tag == "img":
                # not saved: keep the picture's place as text rather than a remote request
                el.set("src", "data:,")
        for base in doc.xpath("//base"):
            base.getparent().remove(base)  # relative paths now mean "beside this file"
        for meta in doc.xpath("//meta[@http-equiv]"):
            if (meta.get("http-equiv") or "").lower() == "content-security-policy":
                meta.getparent().remove(meta)
        return str(lxml_html.tostring(doc, encoding="unicode", doctype="<!doctype html>"))

    return await asyncio.to_thread(rewrite), report
