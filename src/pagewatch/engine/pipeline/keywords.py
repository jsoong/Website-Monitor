"""Keyword rules: parser and evaluator.

One rule per line; lines are OR-ed; matching is case-insensitive.

    word                    substring in the changed text
    "word"                  whole word (or whole phrase)
    a + b                   all terms appear somewhere in this check's changes (AND)
    ... [same_block]        all terms inside one changed block
    ... [near N]            all terms within N words of each other in the changes
    -term                   the rule fails if the term appears in the changes (NOT)
    page(term)              context: the term anywhere on the new page, changed or not
    regex(...)              regular expression
    num(regex) <op> value   the first capture group of each match, parsed as a number,
                            compared with <, <=, >, >= or =
    ... #color              highlight colour for this rule

A rule needs at least one term outside ``page()`` and ``-``, so it can only fire on change.
Changes are ``DiffResult.changed_blocks``: every inserted or edited block of the new page with
the character spans that changed. A term is matched against the *whole* block but only counts
when its match overlaps a changed span: the phrase "in stock" fires when "Out of stock" becomes
"In stock" (the match covers the changed word), and a rule on "sale" does not fire when only an
unrelated timestamp in the same paragraph changed.
"""

from __future__ import annotations

import bisect
import operator
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from pagewatch.engine.pipeline.diff import ChangedBlock

_COLOR = re.compile(r"\s+#([A-Za-z][\w-]*|[0-9a-fA-F]{3,8})\s*$")
_SCOPE = re.compile(r"\s*\[\s*(same_block|near\s+(\d+))\s*\]\s*$", re.IGNORECASE)
_NUM_TAIL = re.compile(r"^\s*(<=|>=|<|>|=)\s*(-?[\d.,]+)\s*$")
_THOUSANDS_COMMA = re.compile(r"^-?\d{1,3}(,\d{3})+(\.\d+)?$")
_THOUSANDS_DOT = re.compile(r"^-?\d{1,3}(\.\d{3}){2,}(,\d+)?$")
_OPS: dict[str, Callable[[float, float], bool]] = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "=": lambda a, b: abs(a - b) < 1e-9,
}


class KeywordSyntaxError(ValueError):
    def __init__(self, message: str, line: int | None = None) -> None:
        super().__init__(f"line {line}: {message}" if line is not None else message)
        self.message = message
        self.line = line


def parse_number(text: str) -> float | None:
    """``1,099`` -> 1099, ``1.099,50`` -> 1099.5, ``12.5`` -> 12.5, ``1,5`` -> 1.5."""
    s = text.strip().replace(" ", "").replace(" ", "")
    s = s.strip("$€£¥").rstrip(".,")
    if not s or not re.search(r"\d", s):
        return None
    try:
        if "," in s and "." in s:
            if s.rfind(",") > s.rfind("."):  # 1.099,50
                return float(s.replace(".", "").replace(",", "."))
            return float(s.replace(",", ""))
        if "," in s:
            if _THOUSANDS_COMMA.match(s):
                return float(s.replace(",", ""))
            return float(s.replace(",", "."))
        if _THOUSANDS_DOT.match(s):
            return float(s.replace(".", ""))
        return float(s)
    except ValueError:
        return None


@dataclass(slots=True)
class Term:
    kind: Literal["substr", "word", "regex", "num"]
    source: str
    pattern: re.Pattern[str]
    negated: bool = False
    page: bool = False
    op: str | None = None
    value: float | None = None

    def spans(self, text: str) -> list[tuple[int, int]]:
        """Character spans of the matches (for ``num`` only those satisfying the comparison)."""
        out: list[tuple[int, int]] = []
        for m in self.pattern.finditer(text):
            if self.kind == "num":
                assert self.op is not None and self.value is not None
                group = m.group(1) if m.re.groups else m.group(0)
                number = parse_number(group or "")
                if number is None or not _OPS[self.op](number, self.value):
                    continue
            out.append(m.span())
        return out

    def found(self, text: str) -> bool:
        return (
            bool(self.spans(text)) if self.kind == "num" else self.pattern.search(text) is not None
        )


@dataclass(slots=True)
class Rule:
    source: str  # the rule as written, without its colour
    terms: list[Term]
    scope: Literal["any", "block", "near"] = "any"
    near: int = 0
    color: str | None = None
    positives: list[Term] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.positives = [t for t in self.terms if not t.negated and not t.page]


# -- parsing ----------------------------------------------------------------------------


def _split_top_level(text: str, sep: str = "+") -> list[str]:
    """Split on ``sep`` outside quotes and parentheses (so ``regex(a+b)`` and ``"c++"`` survive)."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    quote = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text) and depth > 0:
            buf.append(ch + text[i + 1])
            i += 2
            continue
        if ch == '"' and depth == 0:
            quote = not quote
        elif not quote:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth = max(0, depth - 1)
            elif ch == sep and depth == 0:
                parts.append("".join(buf))
                buf = []
                i += 1
                continue
        buf.append(ch)
        i += 1
    if quote:
        raise KeywordSyntaxError('unterminated "quote"')
    if depth:
        raise KeywordSyntaxError("unbalanced parentheses")
    parts.append("".join(buf))
    return parts


def _balanced(text: str, start: int) -> int:
    """Index of the ``)`` matching the ``(`` at ``text[start]``; escapes are skipped."""
    depth = 0
    i = start
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise KeywordSyntaxError("unbalanced parentheses")


def _compile(source: str, what: str) -> re.Pattern[str]:
    try:
        return re.compile(source, re.IGNORECASE | re.MULTILINE)
    except re.error as exc:
        raise KeywordSyntaxError(f"invalid regular expression in {what}: {exc}") from exc


def _parse_term(raw: str) -> Term:
    text = raw.strip()
    if not text:
        raise KeywordSyntaxError("empty term (a stray '+'?)")
    negated = False
    if text.startswith("-") and len(text) > 1:
        negated, text = True, text[1:].strip()
    page = False
    low = text.lower()
    if low.startswith("page(") and text.endswith(")") and _balanced(text, 4) == len(text) - 1:
        page, text = True, text[5:-1].strip()
        if text.startswith("-"):
            raise KeywordSyntaxError("write -page(term), not page(-term)")
        low = text.lower()
    if low.startswith("regex(") and text.endswith(")") and _balanced(text, 5) == len(text) - 1:
        body = text[6:-1]
        if not body:
            raise KeywordSyntaxError("empty regex()")
        return Term("regex", body, _compile(body, "regex()"), negated, page)
    if low.startswith("num("):
        close = _balanced(text, 3)
        body, tail = text[4:close], text[close + 1 :]
        m = _NUM_TAIL.match(tail)
        if not body or not m:
            raise KeywordSyntaxError("num(regex) needs a comparison: num(\\$([\\d,.]+)) < 1200")
        value = parse_number(m.group(2))
        if value is None:
            raise KeywordSyntaxError(f"not a number: {m.group(2)!r}")
        pat = _compile(body, "num()")
        if pat.groups < 1:
            raise KeywordSyntaxError("num(regex) needs a capture group around the number")
        return Term("num", body, pat, negated, page, m.group(1), value)
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        phrase = text[1:-1]
        if not phrase.strip():
            raise KeywordSyntaxError('empty ""')
        pat = re.compile(rf"(?<!\w){re.escape(phrase)}(?!\w)", re.IGNORECASE)
        return Term("word", phrase, pat, negated, page)
    if '"' in text:
        raise KeywordSyntaxError(f"misplaced quote in {text!r}")
    return Term("substr", text, re.compile(re.escape(text), re.IGNORECASE), negated, page)


def parse_rule(line: str) -> Rule:
    text = line.strip()
    color = None
    scope: Literal["any", "block", "near"] = "any"
    near = 0
    for _ in range(2):  # a colour and a scope, in either order
        if (m := _COLOR.search(text)) and color is None:
            color, text = m.group(1), text[: m.start()]
        elif m := _SCOPE.search(text):
            if m.group(1).lower() == "same_block":
                scope = "block"
            else:
                scope, near = "near", int(m.group(2))
            text = text[: m.start()]
    terms = [_parse_term(p) for p in _split_top_level(text)]
    rule = Rule(text.strip(), terms, scope, near, color)
    if not rule.positives:
        raise KeywordSyntaxError(
            "a rule needs at least one term that is not page(...) or -term, "
            "so that it can only fire on change"
        )
    if scope != "any" and len(rule.positives) < 2:
        raise KeywordSyntaxError("[same_block] and [near N] need at least two terms joined by +")
    return rule


def parse_rules(text: str) -> list[Rule]:
    """Parse a rule list. Blank lines and ``//`` comment lines are ignored."""
    rules: list[Rule] = []
    for n, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        try:
            rules.append(parse_rule(stripped))
        except KeywordSyntaxError as exc:
            raise KeywordSyntaxError(exc.message, n) from None
    return rules


# -- evaluation -------------------------------------------------------------------------


def _min_window(occurrences: list[list[int]]) -> int:
    """Smallest ``max - min`` word distance that includes one occurrence of every term."""
    flat = sorted((pos, k) for k, occ in enumerate(occurrences) for pos in occ)
    need = len(occurrences)
    counts = [0] * need
    have = 0
    best = 10**9
    left = 0
    for pos_r, k_r in flat:
        counts[k_r] += 1
        if counts[k_r] == 1:
            have += 1
        while have == need:
            best = min(best, pos_r - flat[left][0])
            k_l = flat[left][1]
            counts[k_l] -= 1
            if counts[k_l] == 0:
                have -= 1
            left += 1
    return best


def _hits(term: Term, block: ChangedBlock) -> list[tuple[int, int]]:
    """Matches of ``term`` in the block that overlap one of its changed spans."""
    return [
        (a, b)
        for a, b in term.spans(block.text)
        if any(a < e and s < b or (a == b and s <= a < e) for s, e in block.spans)
    ]


def _near(terms: Sequence[Term], blocks: Sequence[ChangedBlock], limit: int) -> bool:
    text = "\n".join(b.text for b in blocks)
    starts = [m.start() for m in re.finditer(r"\S+", text)]
    occurrences: list[list[int]] = []
    for t in terms:
        found: list[int] = []
        base = 0
        for cb in blocks:
            for a, _ in _hits(t, cb):
                found.append(max(0, bisect.bisect_right(starts, base + a) - 1))
            base += len(cb.text) + 1
        if not found:
            return False
        occurrences.append(found)
    return _min_window(occurrences) <= limit


def rule_matches(rule: Rule, page_text: str, changes: Sequence[ChangedBlock]) -> bool:
    for t in rule.terms:
        if not t.negated:
            continue
        if t.page:
            if t.found(page_text):
                return False
        elif any(_hits(t, cb) for cb in changes):
            return False
    for t in rule.terms:
        if t.page and not t.negated and not t.found(page_text):
            return False
    if rule.scope == "any":
        return all(any(_hits(t, cb) for cb in changes) for t in rule.positives)
    if rule.scope == "block":
        return any(all(_hits(t, cb) for t in rule.positives) for cb in changes)
    return _near(rule.positives, changes, rule.near)


def evaluate(
    rules: Sequence[Rule], *, page_text: str, changes: Sequence[ChangedBlock]
) -> list[str]:
    """Sources of the rules that fire for these changes (empty: none did)."""
    return [r.source for r in rules if rule_matches(r, page_text, changes)]


def colors(rules: Sequence[Rule]) -> dict[str, str]:
    return {r.source: r.color for r in rules if r.color}
