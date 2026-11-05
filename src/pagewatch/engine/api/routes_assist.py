"""The filter assistant (spec: Desktop UI → Filter assistant)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from pagewatch.engine import assistant
from pagewatch.engine import bookmarks as svc
from pagewatch.engine.api.app_deps import engine_dep
from pagewatch.engine.changes import ConflictError
from pagewatch.engine.core import Engine
from pagewatch.models import FilterAssistOut, FilterAssistRequest

router = APIRouter(tags=["filter-assistant"])
EngineDep = Annotated[Engine, Depends(engine_dep)]


@router.post("/bookmarks/{bookmark_id}/filter-assist", response_model=FilterAssistOut)
async def filter_assist(
    bookmark_id: int, body: FilterAssistRequest, engine: EngineDep
) -> FilterAssistOut:
    """From a selection in the viewer (the DOM path of its element and its text) to up to five
    filter rules: ignore the element, watch only it, ignore text like it, ignore from the page
    start to it, ignore from it to the page end. Nothing is stored; each choice carries a ready
    ``PATCH`` body, and ``/test-filter`` shows what it would do."""
    try:
        return await assistant.run_assist(engine, bookmark_id, body)
    except svc.NotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except svc.InvalidError as exc:
        raise HTTPException(422, str(exc)) from exc
