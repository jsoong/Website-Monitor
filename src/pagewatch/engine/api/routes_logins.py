"""Macros, per-bookmark sign-in state, cookie import, the sign-in window, and write-only secrets."""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Response
from pydantic import ValidationError

from pagewatch.engine.api.app_deps import engine_dep
from pagewatch.engine.core import Engine
from pagewatch.engine.logins.common import macro_out
from pagewatch.engine.logins.experiment import ExperimentError
from pagewatch.engine.logins.service import LoginError, LoginNotFound
from pagewatch.engine.logins.signin import SignInError
from pagewatch.engine.secrets import SecretsUnavailable
from pagewatch.engine.store import repo
from pagewatch.models import (
    SECRET_NAME,
    CookieImportIn,
    CookieStatusOut,
    ExperimentIn,
    ExperimentOut,
    ExperimentReport,
    LoginStatusOut,
    MacroDef,
    MacroOut,
    RecordOut,
    RecordStartIn,
    SecretIn,
    SecretOut,
    SignInOut,
    dumps,
)

router = APIRouter(tags=["logins"])
EngineDep = Annotated[Engine, Depends(engine_dep)]
SecretName = Annotated[str, Path(min_length=1, max_length=64)]


def _not_found(exc: LoginNotFound) -> HTTPException:
    return HTTPException(404, str(exc))


# -- macros -----------------------------------------------------------------------------


@router.get("/macros", response_model=list[MacroOut])
async def list_macros(engine: EngineDep) -> list[MacroOut]:
    return await engine.logins.macros()


# The recorder routes come before /macros/{macro_id}: "record" is not an id.


@router.post("/macros/record", response_model=RecordOut, status_code=201)
async def start_recording(body: RecordStartIn, engine: EngineDep) -> RecordOut:
    """Open Playwright codegen on the owner's desktop; poll the recording until it is done."""
    try:
        return await engine.logins.recorder.start(body.url, body.password_secret_key)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/macros/record/{rec_id}", response_model=RecordOut)
async def get_recording(rec_id: str, engine: EngineDep) -> RecordOut:
    rec = engine.logins.recorder.get(rec_id)
    if rec is None:
        raise HTTPException(404, "no such recording")
    return rec


@router.delete("/macros/record/{rec_id}", response_model=RecordOut)
async def cancel_recording(rec_id: str, engine: EngineDep) -> RecordOut:
    rec = await engine.logins.recorder.cancel(rec_id)
    if rec is None:
        raise HTTPException(404, "no such recording")
    return rec


def _dump(body: MacroDef) -> tuple[str, str, str | None]:
    steps = dumps([s.model_dump(mode="json", exclude_defaults=False) for s in body.steps])
    signal = dumps(body.login_signal.model_dump(mode="json")) if body.login_signal else None
    return body.name, steps, signal


@router.post("/macros", response_model=MacroOut, status_code=201)
async def create_macro(body: MacroDef, engine: EngineDep) -> MacroOut:
    name, steps, signal = _dump(body)
    macro_id = await engine.db.write(lambda c: repo.macro_insert(c, name, steps, signal))
    row = await engine.db.read(lambda c: repo.macro_get(c, macro_id))
    assert row is not None
    return macro_out(row)


@router.get("/macros/{macro_id}", response_model=MacroOut)
async def get_macro(macro_id: int, engine: EngineDep) -> MacroOut:
    row = await engine.db.read(lambda c: repo.macro_get(c, macro_id))
    if row is None:
        raise HTTPException(404, "macro not found")
    try:
        return macro_out(row)
    except ValidationError as exc:
        raise HTTPException(
            422, f"this macro is not valid any more: {exc.errors()[0]['msg']}"
        ) from exc


@router.put("/macros/{macro_id}", response_model=MacroOut)
async def replace_macro(macro_id: int, body: MacroDef, engine: EngineDep) -> MacroOut:
    name, steps, signal = _dump(body)
    found = await engine.db.write(lambda c: repo.macro_update(c, macro_id, name, steps, signal))
    if not found:
        raise HTTPException(404, "macro not found")
    row = await engine.db.read(lambda c: repo.macro_get(c, macro_id))
    assert row is not None
    return macro_out(row)


@router.delete("/macros/{macro_id}", status_code=204)
async def delete_macro(macro_id: int, engine: EngineDep) -> Response:
    """Bookmarks that used it keep running without a macro (their macro_id is cleared)."""
    if not await engine.db.write(lambda c: repo.macro_delete(c, macro_id)):
        raise HTTPException(404, "macro not found")
    return Response(status_code=204)


# -- a bookmark's login -----------------------------------------------------------------


@router.get("/bookmarks/{bookmark_id}/login", response_model=LoginStatusOut)
async def login_status(bookmark_id: int, engine: EngineDep) -> LoginStatusOut:
    try:
        return await engine.logins.status(bookmark_id)
    except LoginNotFound as exc:
        raise _not_found(exc) from exc


@router.post("/bookmarks/{bookmark_id}/login/resume", response_model=LoginStatusOut)
async def resume_login(bookmark_id: int, engine: EngineDep) -> LoginStatusOut:
    """The owner fixed the sign-in: leave needs_login, clear a macro block, check now."""
    try:
        await engine.logins.resume(bookmark_id)
        return await engine.logins.status(bookmark_id)
    except LoginNotFound as exc:
        raise _not_found(exc) from exc


@router.post("/bookmarks/{bookmark_id}/signin", response_model=SignInOut)
async def start_signin(bookmark_id: int, engine: EngineDep) -> SignInOut:
    """Open the visible sign-in window on the bookmark's browser profile."""
    try:
        return await engine.logins.signin.start(bookmark_id)
    except SignInError as exc:
        missing = "not found" in str(exc)
        busy = "already open" in str(exc) or "is running" in str(exc)
        raise HTTPException(404 if missing else 409 if busy else 422, str(exc)) from exc


@router.post("/bookmarks/{bookmark_id}/signin/finish", response_model=SignInOut)
async def finish_signin(bookmark_id: int, engine: EngineDep) -> SignInOut:
    """Close the window, save the session and check at once."""
    return await engine.logins.signin.finish(bookmark_id)


@router.delete("/bookmarks/{bookmark_id}/session", status_code=204)
async def wipe_session(bookmark_id: int, engine: EngineDep) -> Response:
    """ "Wipe session": delete the browser profile and the in-memory cookies."""
    try:
        await engine.logins.wipe_session(bookmark_id)
    except LoginError as exc:
        raise HTTPException(409, str(exc)) from exc
    return Response(status_code=204)


@router.get("/bookmarks/{bookmark_id}/cookies", response_model=CookieStatusOut)
async def cookie_status(bookmark_id: int, engine: EngineDep) -> CookieStatusOut:
    try:
        return (await engine.logins.status(bookmark_id)).cookies
    except LoginNotFound as exc:
        raise _not_found(exc) from exc


@router.post("/bookmarks/{bookmark_id}/cookies", response_model=CookieStatusOut)
async def import_cookies(
    bookmark_id: int, body: CookieImportIn, engine: EngineDep
) -> CookieStatusOut:
    """Import a Netscape cookies.txt for this bookmark. Values are never returned."""
    try:
        return await engine.logins.import_cookies(bookmark_id, body)
    except LoginNotFound as exc:
        raise _not_found(exc) from exc
    except LoginError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.delete("/bookmarks/{bookmark_id}/cookies", status_code=204)
async def remove_cookies(bookmark_id: int, engine: EngineDep) -> Response:
    try:
        await engine.logins.remove_cookies(bookmark_id)
    except LoginNotFound as exc:
        raise _not_found(exc) from exc
    return Response(status_code=204)


# -- secrets (write-only) ---------------------------------------------------------------


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
    """Store a password in the OS credential store. The database keeps only the name."""
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


# -- the portal experiment --------------------------------------------------------------


@router.get("/experiments", response_model=list[ExperimentOut])
async def list_experiments(engine: EngineDep) -> list[ExperimentOut]:
    return engine.logins.experiments.all()


@router.post("/experiments", response_model=ExperimentOut, status_code=201)
async def start_experiment(body: ExperimentIn, engine: EngineDep) -> ExperimentOut:
    """Start the spec's portal experiment on a semi-attended bookmark. It waits for the hand
    sign-in, which is time zero."""
    try:
        return await engine.logins.experiments.create(body)
    except ExperimentError as exc:
        raise HTTPException(404 if "not found" in str(exc) else 422, str(exc)) from exc


@router.get("/experiments/{exp_id}", response_model=ExperimentOut)
async def get_experiment(exp_id: str, engine: EngineDep) -> ExperimentOut:
    exp = engine.logins.experiments.get(exp_id)
    if exp is None:
        raise HTTPException(404, "no such experiment")
    return exp


@router.get("/experiments/{exp_id}/report", response_model=ExperimentReport)
async def experiment_report(exp_id: str, engine: EngineDep) -> ExperimentReport:
    """The findings so far, as text to paste into DECISIONS.md."""
    exp = engine.logins.experiments.get(exp_id)
    if exp is None:
        raise HTTPException(404, "no such experiment")
    return engine.logins.experiments.report(exp)


@router.delete("/experiments/{exp_id}", response_model=ExperimentOut)
async def cancel_experiment(exp_id: str, engine: EngineDep) -> ExperimentOut:
    exp = await engine.logins.experiments.cancel(exp_id)
    if exp is None:
        raise HTTPException(404, "no such experiment")
    return exp
