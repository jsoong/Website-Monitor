"""Helpers for the unattended-operation (M5) tests."""

from __future__ import annotations

from typing import Any

import httpx

from pagewatch.engine.fetch.base import FetchRequest, FetchResult
from pagewatch.models import FetchErrorKind
from tests.support.fakes import ScriptedFetcher, error, ok

PAGE = (
    "<html><body><h1>Notices</h1><p>Nothing new has been posted to this page yet.</p></body></html>"
)
SCHED = {"interval_s": 60, "jitter_pct": 0}


class FakeNetwork:
    """The machine's network: a probe that answers according to ``up``, and a fetcher whose
    requests fail with a connection error while it is down."""

    def __init__(self) -> None:
        self.up = True
        self.probes = 0
        self.body = PAGE

    async def probe(self, url: str, timeout_s: float, proxy: str | None) -> bool:
        self.probes += 1
        return self.up

    def respond(self, request: FetchRequest, call: int) -> FetchResult:
        if not self.up:
            return error(request, FetchErrorKind.CONNECTION, "network is unreachable")
        return ok(request, self.body)

    def fetcher(self) -> ScriptedFetcher:
        return ScriptedFetcher(self.respond)


async def add(client: httpx.AsyncClient, name: str, **kw: Any) -> int:
    kw.setdefault("schedule", SCHED)
    r = await client.post(
        "/bookmarks", json={"url": f"http://{name}.example.test/", "name": name, **kw}
    )
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


async def runs(client: httpx.AsyncClient, bid: int) -> list[dict[str, Any]]:
    """Oldest first."""
    items: list[dict[str, Any]] = (await client.get(f"/bookmarks/{bid}/runs")).json()["items"]
    return list(reversed(items))


async def bookmark(client: httpx.AsyncClient, bid: int) -> dict[str, Any]:
    out: dict[str, Any] = (await client.get(f"/bookmarks/{bid}")).json()
    return out
