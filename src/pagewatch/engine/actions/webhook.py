"""The ``webhook`` action: call a URL when a change passes the gate.

Post-v1 in the spec's owner decisions; built here at the owner's request. The body is a Jinja2
template (sandboxed, strict about unknown names) that must render to JSON, so ``{{ x|tojson }}``
is the way to embed text. Without a template a standard JSON object is sent.

A private alert's default body names the bookmark and the change and nothing else, and the
template variables carry no page content either (see ``content.alert_vars``).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from pagewatch.engine.actions.base import ActionConfigError, AlertContext
from pagewatch.engine.actions.common import get_secret, http_send, parse_params
from pagewatch.engine.actions.content import alert_vars, fetch_content
from pagewatch.engine.actions.templating import TemplateError, render_jinja
from pagewatch.engine.logs import get_logger
from pagewatch.models import WebhookParams

if TYPE_CHECKING:
    from pagewatch.engine.core import Engine

log = get_logger("pagewatch.webhook")


def default_payload(ctx: AlertContext, variables: dict[str, Any]) -> dict[str, Any]:
    bookmark = variables["bookmark"]
    change = variables["change"]
    if ctx.private:
        return {
            "event": "change",
            "private": True,
            "bookmark": {"id": bookmark["id"], "name": bookmark["name"]},
            "change": {"id": change["id"], "detected_at": change["detected_at"]},
        }
    return {
        "event": "change",
        "private": False,
        "bookmark": {k: bookmark[k] for k in ("id", "name", "url")},
        "change": {
            k: change[k]
            for k in (
                "id", "detected_at", "summary", "added_text", "removed_text", "added_words",
                "removed_words", "changed_blocks", "checks_accumulated", "keyword_hits",
            )
        },
    }  # fmt: skip


async def build_body(engine: Engine, ctx: AlertContext, params: WebhookParams) -> bytes | None:
    if params.method == "GET":
        return None
    content = await fetch_content(engine, ctx)
    variables = alert_vars(ctx, content.result if content else None)
    if params.body is None:
        return json.dumps(default_payload(ctx, variables)).encode("utf-8")
    try:
        rendered = render_jinja(params.body, variables)
    except TemplateError as exc:
        raise ActionConfigError(f"webhook body: {exc}") from exc
    try:
        json.loads(rendered)
    except ValueError as exc:
        raise ActionConfigError(
            f"webhook body is not valid JSON after rendering ({exc}); "
            "use {{ value|tojson }} to put text in it"
        ) from exc
    return rendered.encode("utf-8")


async def webhook(engine: Engine, ctx: AlertContext) -> None:
    params = parse_params(WebhookParams, ctx.params, "webhook")
    headers = {"User-Agent": f"PageWatch/{engine.version}", **params.headers}
    body = await build_body(engine, ctx, params)
    if body is not None:
        headers.setdefault("Content-Type", "application/json; charset=utf-8")
    if params.token_key:
        headers["Authorization"] = "Bearer " + await get_secret(engine, params.token_key, "webhook")
    await http_send(
        params.method,
        params.url,
        what="webhook",
        headers=headers,
        body=body,
        timeout_s=params.timeout_s,
        verify_tls=params.verify_tls,
    )
    log.info("webhook_sent", bookmark=ctx.bookmark_id, change=ctx.change_id, private=ctx.private)
