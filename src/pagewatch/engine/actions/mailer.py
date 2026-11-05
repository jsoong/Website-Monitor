"""The ``email`` action: one message per change, SMTP over TLS (aiosmtplib).

Formats (spec): ``simple`` (name, link, one-line summary), ``changes_only`` (just the changed
blocks, marked) and ``full`` (the highlighted page). Every message has a plain-text part and an
HTML part with inline styles only (see ``htmlmail``); a ``template`` replaces the built-in body
with the user's Jinja2 text. The Message-ID is derived from the change and the recipients, so a
job that runs twice after a crash sends a message a mail server can recognise as a duplicate.

A private alert says which bookmark changed and nothing else: no URL, no summary, no changes,
no attachment, and the page content is never read.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime, parseaddr
from typing import TYPE_CHECKING

import aiosmtplib

from pagewatch.engine.actions import htmlmail
from pagewatch.engine.actions.base import ActionConfigError, AlertContext
from pagewatch.engine.actions.common import detected_at, local_time, parse_params
from pagewatch.engine.actions.content import (
    ChangeContent,
    alert_vars,
    fetch_content,
)
from pagewatch.engine.actions.templating import TemplateError, render_jinja
from pagewatch.engine.logs import get_logger
from pagewatch.models import EmailParams, SmtpSettings

if TYPE_CHECKING:
    from pagewatch.engine.core import Engine

log = get_logger("pagewatch.mail")

PRIVATE_TEXT = "Changed. Open PageWatch to review."
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
MAX_TEXT_LINES = 200
PRIORITY_HEADERS = {
    "high": ("1", "high"),
    "low": ("5", "low"),
}


def _is_local_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _need_server(smtp: SmtpSettings) -> None:
    if not smtp.host:
        raise ActionConfigError("email: no SMTP server is set up (Settings, Notifications)")


def _sender(smtp: SmtpSettings) -> str:
    if smtp.from_addr:
        return smtp.from_addr
    if smtp.username and "@" in smtp.username:
        return smtp.username
    raise ActionConfigError(
        "email: no 'from' address (set it under Settings, Notifications, or use a username that "
        "is an address)"
    )


def _recipients(params: EmailParams, smtp: SmtpSettings) -> list[str]:
    to = params.to or smtp.to
    if not to:
        raise ActionConfigError(
            "email: no recipients (set them on the action or under Settings, Notifications)"
        )
    return to


def _subject(params: EmailParams, ctx: AlertContext, content: ChangeContent | None) -> str:
    if params.subject:
        try:
            text = render_jinja(
                params.subject, alert_vars(ctx, content.result if content else None)
            )
        except TemplateError as exc:
            raise ActionConfigError(f"email subject: {exc}") from exc
    else:
        text = f"[PageWatch] {ctx.name} changed"
    return " ".join(text.split())[:300] or "PageWatch"


def _stats_line(ctx: AlertContext) -> str:
    parts: list[str] = []
    if ctx.added_words:
        parts.append(f"{ctx.added_words} words added")
    if ctx.removed_words:
        parts.append(f"{ctx.removed_words} removed")
    if ctx.changed_blocks:
        parts.append(f"{ctx.changed_blocks} blocks changed")
    if ctx.checks_accumulated > 1:
        parts.append(f"over {ctx.checks_accumulated} checks")
    return ", ".join(parts)


def _when(engine: Engine, ctx: AlertContext) -> str:
    return local_time(engine, detected_at(ctx)).strftime("%Y-%m-%d %H:%M")


def text_body(
    engine: Engine, ctx: AlertContext, params: EmailParams, content: ChangeContent | None
) -> str:
    if ctx.private or content is None:
        return f"{ctx.name}\n\n{PRIVATE_TEXT}\n"
    lines = [ctx.name, *([ctx.url] if ctx.url else []), f"Detected {_when(engine, ctx)}", ""]
    stats = _stats_line(ctx)
    if stats:
        lines.append(stats)
    if ctx.keyword_hits:
        lines.append("Keywords: " + ", ".join(ctx.keyword_hits))
    if ctx.summary:
        lines += ["", ctx.summary]
    if params.format != "simple" and content.result.changes_text:
        changed = content.result.changes_text.splitlines()
        shown = [ln for ln in changed if ln[:1] in "+-~>"][:MAX_TEXT_LINES]
        lines += ["", "Changes:", *shown]
        hidden = len([ln for ln in changed if ln[:1] in "+-~>"]) - len(shown)
        if hidden > 0:
            lines.append(f"... and {hidden} more changed blocks (open PageWatch to see them)")
    lines += ["", "-- ", "PageWatch"]
    return "\n".join(lines) + "\n"


def html_body(
    engine: Engine, ctx: AlertContext, params: EmailParams, content: ChangeContent | None
) -> str:
    footer = "Sent by PageWatch."
    if ctx.private or content is None:
        return htmlmail.shell(
            title=ctx.name,
            subtitle_html=htmlmail.esc(f"Detected {_when(engine, ctx)}"),
            meta_html="",
            body_html=f'<p style="margin:0;">{htmlmail.esc(PRIVATE_TEXT)}</p>',
            footer_html=footer,
        )
    meta: list[str] = []
    stats = _stats_line(ctx)
    if stats:
        meta.append(f'<div style="{htmlmail.MUTED}">{htmlmail.esc(stats)}</div>')
    if ctx.keyword_hits:
        chips = " ".join(
            '<span style="display:inline-block;background:#fff3cd;color:#664d03;'
            f'padding:1px 6px;margin:2px 2px 0 0;">{htmlmail.esc(k)}</span>'
            for k in ctx.keyword_hits
        )
        meta.append(f"<div>{chips}</div>")
    body: list[str] = []
    if ctx.summary and params.format == "simple":
        body.append(f'<p style="margin:0 0 8px 0;">{htmlmail.esc(ctx.summary)}</p>')
    result = content.result
    if params.format == "full" and result.full_html:
        body.append(f'<div style="overflow-wrap:break-word;">{result.full_html}</div>')
    elif params.format in ("changes_only", "full") and result.changes_html:
        if params.format == "full":
            body.append(
                f'<p style="{htmlmail.MUTED}">The full page could not be shown; these are the '
                "changes.</p>"
            )
        body.append(result.changes_html)
        if result.changes_hidden:
            body.append(
                f'<p style="{htmlmail.MUTED}">... and {result.changes_hidden} more changed '
                "blocks (open PageWatch to see them).</p>"
            )
    elif ctx.summary and params.format != "simple":
        body.append(f'<p style="margin:0 0 8px 0;">{htmlmail.esc(ctx.summary)}</p>')
    return htmlmail.shell(
        title=ctx.name,
        subtitle_html=(htmlmail.link(ctx.url) + " · " if ctx.url else "")
        + htmlmail.esc(_when(engine, ctx)),
        meta_html="".join(meta),
        body_html="".join(body) or '<p style="margin:0;">The page changed.</p>',
        footer_html=footer,
    )


def _message_id(ctx: AlertContext, recipients: list[str], sender: str, fmt: str) -> str:
    domain = sender.rpartition("@")[2].strip("> ") or "pagewatch.local"
    digest = hashlib.sha1(  # noqa: S324 - an identifier, not a security boundary
        ("|".join(sorted(recipients)) + "|" + fmt).encode()
    ).hexdigest()[:10]
    return f"<pagewatch.{ctx.change_id}.{ctx.bookmark_id}.{digest}@{domain}>"


async def build_message(
    engine: Engine, ctx: AlertContext, params: EmailParams, *, now: datetime
) -> EmailMessage:
    smtp = engine.settings.notifications.smtp
    _need_server(smtp)  # the first thing a new user is missing
    sender = _sender(smtp)
    recipients = _recipients(params, smtp)
    want_full = params.format == "full"
    content = await fetch_content(engine, ctx, want_full=want_full)

    msg = EmailMessage()
    msg["Subject"] = _subject(params, ctx, content)
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Date"] = format_datetime(now)
    msg["Message-ID"] = _message_id(ctx, recipients, sender, params.format)
    msg["X-PageWatch-Bookmark"] = str(ctx.bookmark_id)
    if params.priority in PRIORITY_HEADERS:
        x_priority, importance = PRIORITY_HEADERS[params.priority]
        msg["X-Priority"] = x_priority
        msg["Importance"] = importance

    if params.template and not ctx.private:
        try:
            text = render_jinja(
                params.template, alert_vars(ctx, content.result if content else None)
            )
        except TemplateError as exc:
            raise ActionConfigError(f"email template: {exc}") from exc
        msg.set_content(text)
    else:
        msg.set_content(text_body(engine, ctx, params, content))
        msg.add_alternative(html_body(engine, ctx, params, content), subtype="html")

    if params.attach_screenshot and content is not None:
        await _attach_screenshot(engine, msg, content)
    return msg


async def _attach_screenshot(engine: Engine, msg: EmailMessage, content: ChangeContent) -> None:
    digest = content.result.overlay_hash or content.screenshot_hash
    if not digest:
        return
    data = await asyncio.to_thread(engine.blobs.get, digest)
    if len(data) > MAX_ATTACHMENT_BYTES:
        log.warning("email_attachment_skipped", bytes=len(data))
        return
    name = "screenshot-diff.png" if content.result.overlay_hash else "screenshot.png"
    msg.add_attachment(data, maintype="image", subtype="png", filename=name)


async def deliver(engine: Engine, msg: EmailMessage, smtp: SmtpSettings) -> None:
    """Send over the configured server. TLS is required unless the setting says ``none``, and a
    password is never sent in the clear to a server that is not on this machine."""
    _need_server(smtp)
    password: str | None = None
    if smtp.username:
        if smtp.security == "none" and not _is_local_host(smtp.host):
            raise ActionConfigError(
                "email: refusing to send a password without TLS to a remote server "
                "(choose STARTTLS or SSL)"
            )
        password = await asyncio.to_thread(engine.secrets.get, smtp.password_key)
        if password is None:
            raise ActionConfigError(
                f"email: no stored password named {smtp.password_key!r} for {smtp.username} "
                "(enter it under Settings, Notifications)"
            )
    _, sender = parseaddr(str(msg["From"]))
    recipients = [parseaddr(a)[1] for a in str(msg["To"]).split(",")]
    await aiosmtplib.send(
        msg,
        sender=sender,
        recipients=recipients,
        hostname=smtp.host,
        port=smtp.port,
        username=smtp.username or None,
        password=password,
        use_tls=smtp.security == "ssl",
        start_tls=smtp.security == "starttls",
        validate_certs=smtp.verify_tls,
        timeout=smtp.timeout_s,
    )


async def email(engine: Engine, ctx: AlertContext) -> None:
    params = parse_params(EmailParams, ctx.params, "email")
    msg = await build_message(engine, ctx, params, now=engine.clock.now())
    await deliver(engine, msg, engine.settings.notifications.smtp)
    log.info("email_sent", bookmark=ctx.bookmark_id, change=ctx.change_id, format=params.format)
