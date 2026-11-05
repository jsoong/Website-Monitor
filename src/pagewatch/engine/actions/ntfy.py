"""The ``ntfy`` action: publish to a topic on ntfy.sh or a self-hosted server.

JSON publishing is used (a POST to the server root with the topic in the body) rather than
header-based publishing, because HTTP headers cannot carry non-ASCII bookmark names.

A private alert carries the bookmark name and "Changed. Open PageWatch to review." only, with no
click-through URL. On the public server the topic name is the only protection a topic has unless
it has access control, so the message text is the part to keep short and, for anything signed-in,
private (spec: ntfy exposure).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pagewatch.engine.actions.base import ActionConfigError, AlertContext
from pagewatch.engine.actions.common import (
    get_secret,
    http_send,
    parse_params,
    truncate_utf8,
)
from pagewatch.engine.logs import get_logger
from pagewatch.models import NTFY_PRIORITY, NtfyParams

if TYPE_CHECKING:
    from pagewatch.engine.core import Engine

log = get_logger("pagewatch.ntfy")

MAX_MESSAGE_BYTES = 3500  # ntfy's limit is 4096 bytes; leave room for the JSON envelope
MAX_TITLE_BYTES = 250


def build_payload(ctx: AlertContext, params: NtfyParams, topic: str) -> dict[str, object]:
    body = ctx.body
    if not ctx.private and ctx.keyword_hits:
        body = f"{body}\nKeywords: {', '.join(ctx.keyword_hits)}"
    payload: dict[str, object] = {
        "topic": topic,
        "title": truncate_utf8(ctx.title, MAX_TITLE_BYTES),
        "message": truncate_utf8(body, MAX_MESSAGE_BYTES),
        "priority": NTFY_PRIORITY[params.priority],
    }
    if params.tags:
        payload["tags"] = list(params.tags)
    if not ctx.private and ctx.url.lower().startswith(("http://", "https://")):
        payload["click"] = ctx.url  # tapping the notification opens the page
    return payload


async def ntfy(engine: Engine, ctx: AlertContext) -> None:
    params = parse_params(NtfyParams, ctx.params, "ntfy")
    cfg = engine.settings.notifications.ntfy
    server = (params.server or cfg.server).rstrip("/")
    topic = params.topic or cfg.topic
    if not topic:
        raise ActionConfigError(
            "ntfy: no topic (set it on the action or under Settings, Notifications)"
        )
    headers = {"Content-Type": "application/json"}
    token_key = params.token_key or cfg.token_key
    if token_key:
        headers["Authorization"] = "Bearer " + await get_secret(engine, token_key, "ntfy")
    await http_send(
        "POST",
        server,
        what="ntfy",
        headers=headers,
        body=json.dumps(build_payload(ctx, params, topic)).encode("utf-8"),
        timeout_s=cfg.timeout_s,
    )
    log.info("ntfy_sent", bookmark=ctx.bookmark_id, change=ctx.change_id, private=ctx.private)
