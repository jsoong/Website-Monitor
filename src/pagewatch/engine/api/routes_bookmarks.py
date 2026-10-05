from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from pagewatch.engine import bookmarks as svc
from pagewatch.engine.api.app_deps import engine_dep
from pagewatch.engine.api.serializers import (
    bookmark_out,
    bookmark_summary,
    change_out,
    check_run_out,
)
from pagewatch.engine.core import Engine
from pagewatch.engine.store import repo
from pagewatch.models import (
    BookmarkIn,
    BookmarkOut,
    BookmarkPatch,
    BookmarkSummary,
    BulkRequest,
    BulkResult,
    ChangeOut,
    CheckQueued,
    CheckRunOut,
    Page,
)

router = APIRouter(tags=["bookmarks"])
EngineDep = Annotated[Engine, Depends(engine_dep)]


def _http(exc: Exception) -> HTTPException:
    if isinstance(exc, svc.NotFoundError):
        return HTTPException(404, str(exc))
    return HTTPException(422, str(exc))


async def _row(engine: Engine, bookmark_id: int) -> Any:
    row = await engine.db.read(lambda c: repo.bookmark_get(c, bookmark_id))
    if row is None:
        raise HTTPException(404, "bookmark not found")
    return row


@router.get("/bookmarks", response_model=Page[BookmarkSummary])
async def list_bookmarks(
    engine: EngineDep,
    folder: int | None = None,
    subfolders: bool = True,
    status: str | None = None,
    unread: bool | None = None,
    enabled: bool | None = None,
    q: str | None = None,
    sort: str = "id",
    desc: bool = False,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> Page[BookmarkSummary]:
    def read(conn: Any) -> Any:
        folder_ids = None
        if folder is not None:
            folder_ids = repo.folder_with_descendants(conn, folder) if subfolders else [folder]
        return repo.bookmark_query(
            conn, folder_ids=folder_ids, status=status, unread=unread, enabled=enabled,
            q=q, sort=sort, desc=desc, cursor=cursor, limit=limit,
        )  # fmt: skip

    try:
        rows, nxt, total = await engine.db.read(read)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return Page[BookmarkSummary](
        items=[bookmark_summary(engine, r) for r in rows], next_cursor=nxt, total=total
    )


@router.post("/bookmarks", response_model=BookmarkOut, status_code=201)
async def create_bookmark(body: BookmarkIn, engine: EngineDep) -> BookmarkOut:
    try:
        row = await svc.create(engine, body)
    except (svc.InvalidError, svc.NotFoundError) as exc:
        raise _http(exc) from exc
    return bookmark_out(engine, row)


@router.get("/bookmarks/{bookmark_id}", response_model=BookmarkOut)
async def get_bookmark(bookmark_id: int, engine: EngineDep) -> BookmarkOut:
    return bookmark_out(engine, await _row(engine, bookmark_id))


@router.patch("/bookmarks/{bookmark_id}", response_model=BookmarkOut)
async def patch_bookmark(bookmark_id: int, body: BookmarkPatch, engine: EngineDep) -> BookmarkOut:
    try:
        row = await svc.patch(engine, bookmark_id, body)
    except (svc.InvalidError, svc.NotFoundError) as exc:
        raise _http(exc) from exc
    return bookmark_out(engine, row)


@router.delete("/bookmarks/{bookmark_id}", status_code=204)
async def delete_bookmark(bookmark_id: int, engine: EngineDep) -> Response:
    await _row(engine, bookmark_id)
    await svc.delete(engine, [bookmark_id])
    return Response(status_code=204)


@router.post("/bookmarks/bulk", response_model=BulkResult)
async def bulk(body: BulkRequest, engine: EngineDep) -> BulkResult:
    affected = 0
    try:
        if body.action == "delete":
            affected = await svc.delete(engine, body.ids)
        else:
            if body.action == "update":
                if body.patch is None:
                    raise HTTPException(422, "'update' needs a patch")
                patch = body.patch
            elif body.action == "move":
                patch = (
                    BookmarkPatch(folder_id=body.folder_id)
                    if body.folder_id is not None
                    else BookmarkPatch(move_to_root=True)
                )
            else:
                patch = BookmarkPatch(enabled=body.action == "enable")
            for bid in body.ids:
                try:
                    await svc.patch(engine, bid, patch)
                    affected += 1
                except svc.NotFoundError:
                    continue
    except svc.InvalidError as exc:
        raise _http(exc) from exc
    return BulkResult(affected=affected)


@router.post("/bookmarks/{bookmark_id}/check", response_model=CheckQueued)
async def check_bookmark(bookmark_id: int, engine: EngineDep, force: bool = False) -> CheckQueued:
    await _row(engine, bookmark_id)
    return CheckQueued(queued=engine.check_now([bookmark_id], force=force))


@router.post("/bookmarks/{bookmark_id}/read", response_model=BookmarkOut)
async def mark_read(bookmark_id: int, engine: EngineDep) -> BookmarkOut:
    await _row(engine, bookmark_id)
    await engine.mark_read(bookmark_id)
    return bookmark_out(engine, await _row(engine, bookmark_id))


@router.get("/bookmarks/{bookmark_id}/changes", response_model=Page[ChangeOut])
async def list_changes(
    bookmark_id: int,
    engine: EngineDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    before: int | None = None,
) -> Page[ChangeOut]:
    await _row(engine, bookmark_id)
    rows = await engine.db.read(lambda c: repo.changes_for(c, bookmark_id, limit + 1, before))
    more = len(rows) > limit
    rows = rows[:limit]
    return Page[ChangeOut](
        items=[change_out(r) for r in rows], next_cursor=str(rows[-1]["id"]) if more else None
    )


@router.get("/bookmarks/{bookmark_id}/runs", response_model=Page[CheckRunOut])
async def list_runs(
    bookmark_id: int,
    engine: EngineDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    before: int | None = None,
) -> Page[CheckRunOut]:
    await _row(engine, bookmark_id)
    rows = await engine.db.read(lambda c: repo.check_runs_for(c, bookmark_id, limit + 1, before))
    more = len(rows) > limit
    rows = rows[:limit]
    return Page[CheckRunOut](
        items=[check_run_out(r) for r in rows], next_cursor=str(rows[-1]["id"]) if more else None
    )
