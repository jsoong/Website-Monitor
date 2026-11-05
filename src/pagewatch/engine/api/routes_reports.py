"""Reports (spec: Engine local API, ``POST /reports/{template}``)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response

from pagewatch.engine import bookmarks as svc
from pagewatch.engine import reports
from pagewatch.engine.api.app_deps import engine_dep
from pagewatch.engine.core import Engine
from pagewatch.models import ReportInfo, ReportRequest

router = APIRouter(tags=["reports"])
EngineDep = Annotated[Engine, Depends(engine_dep)]


@router.get("/reports", response_model=list[ReportInfo])
async def list_reports(engine: EngineDep) -> list[ReportInfo]:
    """The built-in reports and any ``<data>/reports/<name>.<format>.j2`` templates."""
    return reports.list_reports(engine)


@router.post("/reports/{template}")
async def make_report(
    template: str, engine: EngineDep, body: ReportRequest | None = None
) -> Response:
    """Render a report. The response body is the document itself (HTML, CSV, JSON or XML).
    ``changed`` lists the changes since the last report; asking moves that marker unless
    ``mark`` is false."""
    req = body or ReportRequest()
    try:
        text, fmt = await reports.generate(
            engine, template, fmt=req.format, since=req.since, mark=req.mark,
            folder_id=req.folder_id,
        )  # fmt: skip
    except svc.NotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except reports.ReportError as exc:
        raise HTTPException(422, str(exc)) from exc
    return Response(
        content=text.encode("utf-8"),
        media_type=f"{reports.MEDIA[fmt]}; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{template}.{fmt}"'},
    )
