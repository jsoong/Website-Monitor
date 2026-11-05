"""Import and export (spec: Engine local API, ``/import`` and ``/export``)."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from pagewatch.engine import bookmarks as svc
from pagewatch.engine import portability
from pagewatch.engine.api.app_deps import engine_dep
from pagewatch.engine.core import Engine
from pagewatch.models import ImportRequest, ImportResult

router = APIRouter(tags=["import-export"])
EngineDep = Annotated[Engine, Depends(engine_dep)]


@router.post("/import", response_model=ImportResult)
async def import_bookmarks(body: ImportRequest, engine: EngineDep) -> ImportResult:
    """Import browser bookmarks (Netscape HTML), a URL list, CSV, XLSX or a PageWatch export.
    Bookmarks whose normalised URL already exists are skipped; ``dry_run`` only reports."""
    try:
        return await portability.run_import(engine, body)
    except (svc.InvalidError, svc.NotFoundError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/export")
async def export_bookmarks(
    engine: EngineDep,
    format: Literal["json", "opml", "csv"] = "json",
    folder: int | None = None,
    ids: Annotated[list[int] | None, Query()] = None,
) -> Response:
    """The configuration as versioned JSON, the feed bookmarks as OPML, or a CSV list.
    Secrets are never included (credentials are keyring entry names)."""
    try:
        body, media, ext = await portability.export_text(engine, format, folder_id=folder, ids=ids)
    except (svc.InvalidError, svc.NotFoundError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return Response(
        content=body.encode("utf-8"),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="pagewatch-export.{ext}"'},
    )
