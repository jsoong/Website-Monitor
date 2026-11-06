"""The default connectivity probe address (Windows' NCSI host) and how the probe uses it."""

from __future__ import annotations

import httpx
import pytest

from pagewatch.engine.power import http_probe, probe_url
from pagewatch.models import Settings

HTTP = "http://www.msftconnecttest.com/connecttest.txt"
HTTPS = "https://www.msftconnecttest.com/connecttest.txt"


def test_the_default_probe_address_is_plain_http() -> None:
    """www.msftconnecttest.com has no valid certificate over https (hostname mismatch), so an
    https default failed every probe and left the engine offline from the first start."""
    assert Settings().connectivity_url == HTTP


@pytest.mark.parametrize(
    ("saved", "probed"),
    [
        (HTTPS, HTTP),  # what earlier versions stored in settings.json / the database
        (
            "HTTPS://WWW.MSFTCONNECTTEST.COM/connecttest.txt",
            "http://WWW.MSFTCONNECTTEST.COM/connecttest.txt",
        ),
        ("https://www.msftconnecttest.com", "http://www.msftconnecttest.com"),
        ("https://www.msftconnecttest.com:443/x?y=1", "http://www.msftconnecttest.com:443/x?y=1"),
        (f"  {HTTPS}  ", HTTP),
        (HTTP, HTTP),
        ("https://example.com/ping", "https://example.com/ping"),  # anyone else's https is theirs
        (
            "https://www.msftconnecttest.com.evil.test/x",
            "https://www.msftconnecttest.com.evil.test/x",
        ),
        (
            "https://proxy.test/www.msftconnecttest.com",
            "https://proxy.test/www.msftconnecttest.com",
        ),
    ],
)
def test_probe_url_rewrites_only_the_old_default(saved: str, probed: str) -> None:
    assert probe_url(saved) == probed


@pytest.fixture
def heads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    async def head(self: httpx.AsyncClient, url: str, **kw: object) -> httpx.Response:
        seen.append(str(url))
        return httpx.Response(204)

    monkeypatch.setattr(httpx.AsyncClient, "head", head)
    return seen


async def test_the_probe_requests_http_even_for_a_saved_https_default(heads: list[str]) -> None:
    assert await http_probe(HTTPS, 1.0, None) is True
    assert await http_probe(HTTP, 1.0, None) is True
    assert await http_probe("https://example.com/ping", 1.0, None) is True
    assert heads == [HTTP, HTTP, "https://example.com/ping"]


async def test_an_unreachable_probe_target_still_means_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def head(self: httpx.AsyncClient, url: str, **kw: object) -> httpx.Response:
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(httpx.AsyncClient, "head", head)
    assert await http_probe(HTTPS, 1.0, None) is False
