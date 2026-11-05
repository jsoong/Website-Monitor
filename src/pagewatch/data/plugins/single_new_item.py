"""More-than-one-new-item alert: stay quiet unless several new items appear.

Some pages gain one entry at a time and you only care when a batch arrives. This plugin counts
the page's text blocks that were not on the page before and suppresses the alert unless there
are at least two (set ``min_new=3`` in the bookmark's Info 1 field for a higher bar). It uses the
``compare`` hook, so it has the last word: it also lets a batch through when the built-in rules
(keywords, word thresholds) would have said no. The new version is stored either way.
"""

from pagewatch.plugin_api import Verdict


def compare(ctx, old, new):
    try:
        needed = max(1, int(ctx.option("min_new", 2)))
    except ValueError:
        needed = 2
    seen = {block.lower() for block in old}
    fresh = [block for block in new if block.lower() not in seen]
    if len(fresh) >= needed:
        return Verdict.change()
    return Verdict.no_change(f"{len(fresh)} new item(s), need {needed}")
