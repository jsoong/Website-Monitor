"""Read-only views for the desktop UI's Problems list and Settings capacity page."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from pagewatch.engine import capacity, problems
from pagewatch.engine.api.app_deps import engine_dep
from pagewatch.engine.core import Engine
from pagewatch.models import CapacityOut, ProblemsOut

router = APIRouter(tags=["problems"])
EngineDep = Annotated[Engine, Depends(engine_dep)]


@router.get("/problems", response_model=ProblemsOut)
async def get_problems(
    engine: EngineDep,
    limit: Annotated[int, Query(ge=1, le=problems.MAX_LIMIT)] = problems.DEFAULT_LIMIT,
) -> ProblemsOut:
    """Failing bookmarks (with the last error) and actions that gave up, newest first."""
    return await problems.list_problems(engine, limit)


@router.get("/capacity", response_model=CapacityOut)
async def get_capacity(
    engine: EngineDep,
    hosts: Annotated[int, Query(ge=1, le=500)] = capacity.HOSTS_SHOWN,
) -> CapacityOut:
    """Scheduled demand per worker pool and per host, against what each can do."""
    return await capacity.compute(engine, hosts_shown=hosts)
