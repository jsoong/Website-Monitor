"""Backup, restore and shutdown (spec: ``POST /backup``, ``POST /restore``; ``POST /shutdown``
is an addition, see DECISIONS)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from pagewatch.engine.api.app_deps import engine_dep
from pagewatch.engine.core import Engine
from pagewatch.engine.store.backup import BackupError
from pagewatch.models import BackupOut, BackupRequest, RestoreOut, RestoreRequest, ShutdownOut

router = APIRouter(tags=["maintenance"])


def _local_path(text: str) -> Path:
    return Path(text).expanduser()


EngineDep = Annotated[Engine, Depends(engine_dep)]


@router.post("/backup", response_model=BackupOut)
async def backup(engine: EngineDep, body: BackupRequest | None = None) -> BackupOut:
    """Write a backup zip now: into ``backups\\`` unless ``path`` says where."""
    body = body or BackupRequest()
    dest = _local_path(body.path) if body.path else None
    try:
        info = await engine.backup_now(include_blobs=body.include_blobs, dest=dest)
    except BackupError as exc:
        raise HTTPException(409, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(500, f"the backup could not be written: {exc}") from exc
    return BackupOut(
        path=str(info.path), size_bytes=info.size_bytes, created_at=info.created_at,
        include_blobs=info.include_blobs, schema_version=info.schema_version,
    )  # fmt: skip


@router.post("/restore", response_model=RestoreOut, status_code=202)
async def restore(body: RestoreRequest, engine: EngineDep) -> RestoreOut:
    """Validate a backup zip, stage it and restart the engine, which applies it on the way up.
    Nothing is changed unless the zip passes every check."""
    try:
        manifest = await engine.restore(_local_path(body.path))
    except BackupError as exc:
        raise HTTPException(422, str(exc)) from exc
    return RestoreOut(
        staged=True, restart=True,
        message=f"restoring the backup made {manifest.created_at}; the engine is restarting",
    )  # fmt: skip


@router.post("/shutdown", response_model=ShutdownOut, status_code=202)
async def shutdown(engine: EngineDep, background: BackgroundTasks) -> ShutdownOut:
    """Quit cleanly, as on Ctrl+C: dispatch stops, in-flight checks get up to 10 s, the browser is
    closed and the database checkpointed. The exit code is 0, so a supervising Task Scheduler task
    does not start the engine again. Like every route it needs the bearer token. The stop is
    scheduled to run once the response has been sent, so the caller always gets its answer."""
    background.add_task(engine.request_stop)
    return ShutdownOut(
        stopping=True, message="the engine is quitting: in-flight checks are given up to 10 s"
    )
