"""Plugins (spec: Extensibility): what is installed, and installing the shipped examples."""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from pagewatch.engine import bookmarks as svc
from pagewatch.engine.api.app_deps import engine_dep
from pagewatch.engine.core import Engine
from pagewatch.engine.plugins import PluginExistsError
from pagewatch.models import PluginInfo, PluginInstallRequest

router = APIRouter(tags=["plugins"])
EngineDep = Annotated[Engine, Depends(engine_dep)]


@router.get("/plugins", response_model=list[PluginInfo])
async def list_plugins(engine: EngineDep) -> list[PluginInfo]:
    """The plugins in the data folder (with the hooks each defines, or why it cannot load) and
    the examples PageWatch ships. Installed files are imported to find their hooks, so this runs
    in a thread."""
    return await asyncio.to_thread(engine.plugins.catalogue)


@router.post("/plugins/install", response_model=PluginInfo, status_code=201)
async def install_plugin(body: PluginInstallRequest, engine: EngineDep) -> PluginInfo:
    """Copy a shipped example into the plugins folder."""
    try:
        return await asyncio.to_thread(
            lambda: engine.plugins.install(body.name, overwrite=body.overwrite)
        )
    except svc.NotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PluginExistsError as exc:
        raise HTTPException(409, str(exc)) from exc
