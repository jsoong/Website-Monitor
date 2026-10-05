from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError

from pagewatch.engine.api.app_deps import engine_dep
from pagewatch.engine.clock import iso, parse_iso
from pagewatch.engine.core import Engine
from pagewatch.engine.store import repo
from pagewatch.models import AutowatchRequest, AutowatchState, CheckQueued, CheckRequest, Settings

router = APIRouter(tags=["checks"])
EngineDep = Annotated[Engine, Depends(engine_dep)]


@router.post("/check", response_model=CheckQueued)
async def check(body: CheckRequest, engine: EngineDep) -> CheckQueued:
    """Check a list of bookmark ids, a folder (with its subfolders), or everything."""
    if body.all:
        ids = await engine.db.read(repo.bookmark_all_ids)
    elif body.ids:
        ids = body.ids
    elif body.folder_id is not None:
        ids = await engine.db.read(
            lambda c: repo.bookmark_ids_in(c, repo.folder_with_descendants(c, body.folder_id or 0))
        )
    else:
        raise HTTPException(422, "give 'ids', 'folder_id' or 'all'")
    return CheckQueued(queued=engine.check_now(ids, force=body.force))


def _state(engine: Engine) -> AutowatchState:
    until = engine.scheduler.paused_until
    return AutowatchState(
        state="paused" if engine.scheduler.paused else "running",
        until=iso(until) if until else None,
    )


@router.get("/autowatch", response_model=AutowatchState)
async def get_autowatch(engine: EngineDep) -> AutowatchState:
    return _state(engine)


@router.post("/autowatch", response_model=AutowatchState)
async def set_autowatch(body: AutowatchRequest, engine: EngineDep) -> AutowatchState:
    until = None
    if body.state == "paused" and body.until:
        try:
            until = parse_iso(body.until)
        except ValueError as exc:
            raise HTTPException(422, "'until' must be an ISO-8601 UTC timestamp") from exc
        if until <= engine.clock.now() + timedelta(seconds=0):
            raise HTTPException(422, "'until' is in the past")
    await engine.set_autowatch(body.state, until)
    return _state(engine)


@router.get("/settings", response_model=Settings)
async def get_settings(engine: EngineDep) -> Settings:
    return engine.settings


@router.put("/settings", response_model=Settings)
async def put_settings(patch: dict[str, object], engine: EngineDep) -> Settings:
    """Partial update: only the keys given change."""
    unknown = set(patch) - set(Settings.model_fields)
    if unknown:
        raise HTTPException(422, f"unknown setting(s): {sorted(unknown)}")
    try:
        return await engine.update_settings(dict(patch))
    except ValidationError as exc:
        raise HTTPException(422, str(exc.errors()[0]["msg"])) from exc
