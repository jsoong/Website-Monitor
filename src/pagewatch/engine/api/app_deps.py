"""Shared FastAPI dependencies (kept apart from app.py to avoid import cycles)."""

from __future__ import annotations

from fastapi import Request

from pagewatch.engine.core import Engine


def engine_dep(request: Request) -> Engine:
    engine: Engine = request.app.state.engine
    return engine
