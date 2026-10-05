from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from pagewatch.engine import bookmarks as svc
from pagewatch.engine import changes as ops
from pagewatch.engine.api.app_deps import engine_dep
from pagewatch.engine.core import Engine
from pagewatch.engine.pipeline.core import RenderResult
from pagewatch.models import (
    FalsePositiveOut,
    PreviewOut,
    PreviewRequest,
    RenderOut,
    TestFilterOut,
    TestFilterRequest,
)

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


def _html_or_json(out: RenderOut, fmt: str) -> Response | RenderOut:
    if fmt == "json":
        return out
    return Response(
        content=out.html,
        media_type="text/html",
        headers={
            "X-PageWatch-View": out.view,
            "X-PageWatch-Identical": str(out.identical).lower(),
            "X-PageWatch-Degraded": str(out.degraded).lower(),
        },
    )


def _respond(res: RenderResult, view: str, fmt: str) -> Response | RenderOut:
    """HTML or JSON for every view; ``format=png`` returns the screenshot diff's overlay picture."""
    if fmt == "png":
        if view != "screenshot":
            raise HTTPException(422, "format=png is only available with view=screenshot")
        if res.png is None:
            raise HTTPException(404, "no screenshot was stored")
        return Response(
            content=res.png,
            media_type="image/png",
            headers={
                "X-PageWatch-View": "screenshot",
                "X-PageWatch-Identical": str(res.identical).lower(),
                "X-PageWatch-Regions": str(res.stats.get("regions", 0)),
                "X-PageWatch-Changed-Pixels": str(res.stats.get("changed_pixels", 0)),
            },
        )
    return _html_or_json(ops._out(res), fmt)


@router.get("/changes/{change_id}/render", response_model=None)
async def render_change(
    change_id: int,
    engine: EngineDep,
    view: Literal["highlight", "text", "new", "old", "screenshot"] = "highlight",
    format: Literal["html", "json", "png"] = "html",
    images: bool = False,
    context: Annotated[int | None, Query(ge=0, le=100)] = None,
) -> Response | RenderOut:
    """One change's own gate diff (the history view), as sanitised HTML (or ``format=json``).
    ``view=screenshot`` is the visual diff: its overlay as ``format=png``, or wrapped in HTML."""
    try:
        res = await ops.render_change_raw(
            engine, change_id, view, allow_remote=images, context=context
        )
    except (svc.NotFoundError, ops.ConflictError, svc.InvalidError) as exc:
        raise _http(exc) from exc
    return _respond(res, view, format)


@router.get("/bookmarks/{bookmark_id}/diff", response_model=None)
async def unread_diff(
    bookmark_id: int,
    engine: EngineDep,
    view: Literal["highlight", "text", "new", "old", "screenshot"] = "highlight",
    format: Literal["html", "json", "png"] = "html",
    images: bool = False,
    context: Annotated[int | None, Query(ge=0, le=100)] = None,
) -> Response | RenderOut:
    """The viewer's default: everything unread (last-read version -> latest). ``view=new`` and
    ``view=old`` return the stored pages unmarked; ``view=screenshot`` compares the last-read
    and latest screenshots (``format=png`` for the overlay picture)."""
    try:
        if view in ("new", "old"):
            if format == "png":
                raise HTTPException(422, "format=png is only available with view=screenshot")
            out = await ops.render_version(engine, bookmark_id, view, allow_remote=images)
            return _html_or_json(out, format)
        res = await ops.render_unread_raw(
            engine, bookmark_id, view, allow_remote=images, context=context
        )
    except (svc.NotFoundError, ops.ConflictError, svc.InvalidError) as exc:
        raise _http(exc) from exc
    return _respond(res, view, format)


@router.post("/preview", response_model=PreviewOut)
async def preview(body: PreviewRequest, engine: EngineDep) -> PreviewOut:
    """Trial fetch of a URL and options for the add-bookmark assistant; nothing is stored."""
    try:
        return await ops.run_preview(engine, body)
    except (svc.InvalidError, svc.NotFoundError) as exc:
        raise _http(exc) from exc
