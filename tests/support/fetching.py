"""Build a ``FetchRequest`` for a fetcher test without an engine."""

from __future__ import annotations

from typing import Any

from pagewatch.engine.config import Resolved
from pagewatch.engine.fetch.base import FetchRequest
from pagewatch.models import (
    ActionsConfig,
    FetchConfig,
    FilterConfig,
    GateConfig,
    ScheduleConfig,
    Settings,
)


def make_request(
    url: str,
    *,
    settings: Settings | None = None,
    bookmark_id: int | None = None,
    force: bool = True,
    **fetch: Any,
) -> FetchRequest:
    fetch.setdefault("timeout_s", 5)
    return FetchRequest(
        url=url,
        resolved=Resolved(
            schedule=ScheduleConfig(),
            fetch=FetchConfig.model_validate(fetch),
            filter=FilterConfig(),
            gate=GateConfig(),
            actions=ActionsConfig(),
            overrides={},
        ),
        settings=settings or Settings(),
        force=force,
        bookmark_id=bookmark_id,
    )
