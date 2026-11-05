"""Page-not-changed alert: tell me when a page has not changed for N days.

Assign it to a bookmark and set the number of days in Info 1 (the default is 7):

    days=14

Useful for pages that should keep moving (a blog, a price list, a status page). The alert is a
notification, raised once per quiet period; the next real change starts a new one. It uses
the ``after_check`` hook and keeps the date of the last change in the plugin state.
"""

from datetime import datetime, timedelta

DEFAULT_DAYS = 7


def after_check(ctx, run):
    if run.outcome == "error":
        return  # a failing site is a different problem
    now = ctx.now()
    stamp = ctx.state.get("last_change")
    if run.outcome == "first" or run.changed:  # new content, alerted on or not
        ctx.state.update(last_change=now.isoformat(), alerted=False)
        return
    if stamp is None:
        ctx.state.update(last_change=now.isoformat(), alerted=False)  # start counting now
        return
    try:
        days = float(ctx.option("days", DEFAULT_DAYS))
    except ValueError:
        days = DEFAULT_DAYS
    quiet = now - datetime.fromisoformat(stamp)
    if quiet >= timedelta(days=days) and not ctx.state.get("alerted"):
        ctx.state["alerted"] = True
        ctx.notify(
            f"{ctx.bookmark.name}: no change for {quiet.days} days",
            f"This page has not changed since {stamp[:10]}.",
        )
