"""Action jobs (the Problems list), test notifications and write-only secrets."""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response

from pagewatch.engine.actions.testing import send_test
from pagewatch.engine.api.app_deps import engine_dep
from pagewatch.engine.core import Engine
from pagewatch.engine.secrets import SecretsUnavailable
from pagewatch.engine.store import action_jobs
from pagewatch.models import (
    SECRET_NAME,
    ActionJobOut,
    SecretIn,
    SecretOut,
    TestNotificationOut,
    TestNotificationRequest,
)

router = APIRouter(tags=["actions"])
EngineDep = Annotated[Engine, Depends(engine_dep)]
SecretName = Annotated[str, Path(min_length=1, max_length=64)]


def _job_out(row: sqlite3.Row) -> ActionJobOut:
    return ActionJobOut(
        id=row["id"],
        change_id=row["change_id"],
        bookmark_id=row["bookmark_id"],
        bookmark_name=row["bookmark_name"],
        action_index=row["action_index"],
        action_type=row["action_type"],
        status=row["status"],
        attempts=row["attempts"],
        next_attempt_at=row["next_attempt_at"],
        last_error=row["last_error"],
        detected_at=row["detected_at"],
    )


@router.get("/action-jobs", response_model=list[ActionJobOut])
async def list_action_jobs(
    engine: EngineDep,
    status: Literal["queued", "done", "failed", "all"] = "failed",
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[ActionJobOut]:
    """Action jobs, newest first. The default, ``failed``, is the Problems list."""
    wanted = None if status == "all" else status
    rows = await engine.db.read(lambda c: action_jobs.action_jobs_list(c, wanted, limit))
    return [_job_out(r) for r in rows]


@router.post("/action-jobs/{job_id}/retry", response_model=ActionJobOut)
async def retry_action_job(job_id: int, engine: EngineDep) -> ActionJobOut:
    """Run a failed job again, with a fresh set of attempts."""
    ok = await engine.db.write(lambda c: action_jobs.action_job_retry(c, job_id))
    if not ok:
        raise HTTPException(404, f"no failed action job {job_id}")
    engine.actions.kick()
    row = await engine.db.read(lambda c: action_jobs.action_job_view(c, job_id))
    assert row is not None
    return _job_out(row)


@router.delete("/action-jobs/{job_id}", status_code=204)
async def dismiss_action_job(job_id: int, engine: EngineDep) -> Response:
    """Remove a failed job from the Problems list."""
    ok = await engine.db.write(lambda c: action_jobs.action_job_dismiss(c, job_id))
    if not ok:
        raise HTTPException(404, f"no failed action job {job_id}")
    return Response(status_code=204)


@router.post("/notifications/test", response_model=TestNotificationOut)
async def test_notification(
    body: TestNotificationRequest, engine: EngineDep
) -> TestNotificationOut:
    """Send a sample through email, ntfy or a webhook with the saved settings."""
    return await send_test(engine, body)


def _check_name(name: str) -> str:
    if not SECRET_NAME.match(name):
        raise HTTPException(422, "a secret name uses letters, digits and _ . : - (at most 64)")
    return name


@router.get("/secrets/{name}", response_model=SecretOut)
async def get_secret(name: SecretName, engine: EngineDep) -> SecretOut:
    """Whether a secret is stored. The value is never returned."""
    _check_name(name)
    value = await asyncio.to_thread(engine.secrets.get, name)
    return SecretOut(name=name, exists=value is not None, available=engine.secrets.writable)


@router.put("/secrets/{name}", response_model=SecretOut)
async def put_secret(name: SecretName, body: SecretIn, engine: EngineDep) -> SecretOut:
    """Store a secret (an SMTP password, an access token) in the OS credential store. The
    database keeps only the name."""
    _check_name(name)
    try:
        await asyncio.to_thread(engine.secrets.set, name, body.value)
    except SecretsUnavailable as exc:
        raise HTTPException(501, str(exc)) from exc
    return SecretOut(name=name, exists=True, available=True)


@router.delete("/secrets/{name}", response_model=SecretOut)
async def delete_secret(name: SecretName, engine: EngineDep) -> SecretOut:
    _check_name(name)
    try:
        await asyncio.to_thread(engine.secrets.delete, name)
    except SecretsUnavailable as exc:
        raise HTTPException(501, str(exc)) from exc
    return SecretOut(name=name, exists=False, available=engine.secrets.writable)
