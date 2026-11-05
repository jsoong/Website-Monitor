"""Send a sample notification with the saved settings ("Send test" in the UI).

It runs the real action against a made-up change (id 0, no stored content), so a wrong server,
password or topic shows up now rather than when the first real alert fails. Nothing is stored
and no action job is created.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pagewatch.engine.actions.base import Action, ActionUnavailable, AlertContext
from pagewatch.engine.actions.mailer import email
from pagewatch.engine.actions.ntfy import ntfy
from pagewatch.engine.actions.webhook import webhook
from pagewatch.engine.clock import iso
from pagewatch.engine.logs import get_logger
from pagewatch.models import TestNotificationOut, TestNotificationRequest

if TYPE_CHECKING:
    from pagewatch.engine.core import Engine

log = get_logger("pagewatch.notify_test")

CHANNELS: dict[str, Action] = {"email": email, "ntfy": ntfy, "webhook": webhook}
SAMPLE = "This is a test notification from PageWatch. If you can read it, alerts will arrive."


async def send_test(engine: Engine, req: TestNotificationRequest) -> TestNotificationOut:
    ctx = AlertContext(
        bookmark_id=0,
        name="PageWatch test",
        url="",
        info=(None, None, None),
        change_id=0,  # no stored change: the actions build a sample instead of reading blobs
        detected_at=iso(engine.clock.now()),
        summary=SAMPLE,
        added_words=None,
        removed_words=None,
        changed_blocks=None,
        checks_accumulated=1,
        keyword_hits=[],
        private=False,
        params=dict(req.params),
    )
    try:
        await CHANNELS[req.channel](engine, ctx)
    except ActionUnavailable as exc:  # includes ActionConfigError: a setting is missing or wrong
        return TestNotificationOut(ok=False, detail=str(exc))
    except Exception as exc:
        log.info("test_notification_failed", channel=req.channel, error=type(exc).__name__)
        return TestNotificationOut(ok=False, detail=f"{type(exc).__name__}: {exc}"[:500])
    return TestNotificationOut(ok=True, detail="Sent. Check the destination.")
