import asyncio
import json

import httpx
import pytest
import websockets

from pagewatch.engine.core import Engine


async def test_health_requires_the_token(api: tuple[str, str]) -> None:
    base, token = api
    async with httpx.AsyncClient(base_url=base) as c:
        assert (await c.get("/health")).status_code == 401
        assert (await c.get("/health", headers={"Authorization": "Bearer nope"})).status_code == 401
        assert (
            await c.get("/health", headers={"Authorization": f"Basic {token}"})
        ).status_code == 401
        ok = await c.get("/health", headers={"Authorization": f"Bearer {token}"})
        assert ok.status_code == 200
        body = ok.json()
        assert body["version"] and body["pid"] > 0 and body["rss_mb"] > 0
        assert body["bookmarks"] == 0 and body["autowatch"]["state"] == "running"


async def test_browser_origin_and_foreign_host_rejected_even_with_token(
    client: httpx.AsyncClient,
) -> None:
    r = await client.get("/health", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = await client.get("/health", headers={"Host": "evil.example"})
    assert r.status_code == 403
    assert (await client.get("/health", headers={"Host": "localhost:1234"})).status_code == 200


async def test_unknown_route_still_needs_token(api: tuple[str, str]) -> None:
    async with httpx.AsyncClient(base_url=api[0]) as c:
        assert (await c.get("/bookmarks")).status_code == 401


async def test_websocket_token_in_first_message(api: tuple[str, str], engine: Engine) -> None:
    base, token = api
    url = base.replace("http", "ws") + "/events"

    async with websockets.connect(url) as ws:  # wrong token
        await ws.send(json.dumps({"token": "wrong"}))
        with pytest.raises(websockets.ConnectionClosed) as exc:
            await asyncio.wait_for(ws.recv(), 5)
        assert exc.value.rcvd is not None and exc.value.rcvd.code == 4401

    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"token": token}))
        hello = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert hello["type"] == "hello"
        engine.events.publish("engine_state", {"x": 1})
        event = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert event["type"] == "engine_state" and event["data"] == {"x": 1}
    for _ in range(50):  # server notices the disconnect
        if engine.events.subscribers == 0:
            break
        await asyncio.sleep(0.02)
    assert engine.events.subscribers == 0


async def test_websocket_with_browser_origin_is_refused(api: tuple[str, str]) -> None:
    url = api[0].replace("http", "ws") + "/events"
    with pytest.raises(websockets.InvalidStatus):
        async with websockets.connect(url, origin="https://evil.example"):
            pass
