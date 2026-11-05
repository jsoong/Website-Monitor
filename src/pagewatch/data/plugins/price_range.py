"""Price-range alert: alert when a price in the changed text is inside a range.

Install it, assign it to a bookmark, and put the range in the bookmark's Info 1 field:

    min=100 max=250

Either bound may be left out (``max=1200`` alerts on any price up to 1200). Amounts are the
numbers right after a currency sign ($, EUR sign, pound or yen sign), so a year or a stock count
is never mistaken for a price. It uses the ``check_keywords`` hook, so it is OR-ed with the
bookmark's own keyword rules: either one raising a hit makes an alert.
"""

import re

_PRICE = re.compile(r"[$€£¥]\s*(\d[\d.,]*)")


def _bound(ctx, name):
    raw = ctx.option(name)
    if raw is None:
        return None
    values = ctx.helpers.numbers(raw)
    return values[0] if values else None


def check_keywords(ctx, added_text):
    low, high = _bound(ctx, "min"), _bound(ctx, "max")
    if low is None and high is None:
        return None  # nothing configured: defer to the built-in keyword rules
    for match in _PRICE.finditer(added_text):
        for price in ctx.helpers.numbers(match.group(1)):
            if (low is None or price >= low) and (high is None or price <= high):
                ctx.log(f"price {price:g} is inside {low}..{high}")
                return True
    return False
