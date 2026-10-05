from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from starlette.types import Message

from pagewatch.engine.api.app_deps import engine_dep
from pagewatch.engine.core import Engine
from pagewatch.models import EngineEvent, HealthOut

router = APIRouter()
AUTH_TIMEOUT_S = 5.0


@router.get("/health", response_model=HealthOut)
async def health(engine: Annotated[Engine, Depends(engine_dep)]) -> HealthOut:
    return await engine.health()


@router.websocket("/events")
async def events(ws: WebSocket) -> None:
    """Push channel. The first client message must be ``{"token": "<bearer token>"}``."""
    engine: Engine = ws.app.state.engine
    await ws.accept()
    try:
        first = await asyncio.wait_for(ws.receive_json(), timeout=AUTH_TIMEOUT_S)
    except (TimeoutError, WebSocketDisconnect, ValueError, KeyError):
        await ws.close(code=1008)
        return
    if not engine.check_token(str(first.get("token", "")) if isinstance(first, dict) else ""):
        await ws.close(code=4401)
        return
    sub = engine.events.subscribe()
    # Race the event queue against the socket's receive side: that is how a client
    # disconnect (or a stray message) is noticed while the engine is otherwise quiet.
    get_task: asyncio.Task[EngineEvent] = asyncio.ensure_future(sub.queue.get())
    recv_task: asyncio.Task[Message] = asyncio.ensure_future(ws.receive())
    try:
        await ws.send_json({"type": "hello", "data": {"version": engine.version}})
        while True:
            done, _ = await asyncio.wait({get_task, recv_task}, return_when=asyncio.FIRST_COMPLETED)
            if recv_task in done:
                if recv_task.result()["type"] == "websocket.disconnect":
                    break
                recv_task = asyncio.ensure_future(ws.receive())  # ignore client chatter
            if get_task in done:
                await ws.send_json(get_task.result().model_dump(mode="json"))
                get_task = asyncio.ensure_future(sub.queue.get())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        get_task.cancel()
        recv_task.cancel()
        sub.close()
