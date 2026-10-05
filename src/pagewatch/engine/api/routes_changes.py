from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from pagewatch.engine import bookmarks as svc
from pagewatch.engine import changes as ops
from pagewatch.engine.api.app_deps import engine_dep
from pagewatch.engine.core import Engine
from pagewatch.models import FalsePositiveOut, TestFilterOut, TestFilterRequest

router = APIRouter(tags=["changes"])
EngineDep = Annotated[Engine, Depends(engine_dep)]


def _http(exc: Exception) -> HTTPException:
    if isinstance(exc, svc.NotFoundError):
        return HTTPException(404, str(exc))
    if isinstance(exc, ops.ConflictError):
        return HTTPException(409, str(exc))
    return HTTPException(422, str(exc))


@router.post("/bookmarks/{bookmark_id}/test-filter", response_model=TestFilterOut)
async def test_filter(
    bookmark_id: int, body: TestFilterRequest, engine: EngineDep
) -> TestFilterOut:
    """Run the full pipeline on the stored baseline and latest raw content with a candidate
    configuration. Persists nothing."""
    try:
        return await ops.run_test_filter(engine, bookmark_id, body)
    except (svc.NotFoundError, svc.InvalidError, ops.ConflictError) as exc:
        raise _http(exc) from exc


@router.post("/changes/{change_id}/false-positive", response_model=FalsePositiveOut)
async def false_positive(change_id: int, engine: EngineDep) -> FalsePositiveOut:
    """Flag a change as a false positive; returns proposed automatic filters."""
    try:
        return await ops.flag_false_positive(engine, change_id)
    except (svc.NotFoundError, ops.ConflictError) as exc:
        raise _http(exc) from exc
