"""Two-stage diff.

Stage 1 aligns the two *block* sequences by text hash (so wrapper-element changes do not
disturb alignment) into equal / insert / delete / replace runs. Blocks that were deleted in
one place and inserted elsewhere with identical text become ``mov`` ops and do not count as
changes in Standard mode. Stage 2 word-diffs inside each replace run.

Worst cases are bounded: a replace run above ``MAX_TOKENS`` tokens skips the word diff, more
than ``MAX_BLOCKS`` blocks diffs at block level only, and a diff that exhausts its time
budget finishes at block level. In every such case the result has ``degraded = True``.

The output follows the spec's diff format; ranges are *inclusive* ``[first, last]``.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass
from typing import Any

from pagewatch.engine.pipeline.differs import Differ, Opcode, get_differ

BUDGET_S = 2.0
MAX_BLOCKS = 20_000
MAX_TOKENS = 5_000
# Indel alignment keeps a bit matrix of len(old) * len(new) cells: bound it so a pathological
# page cannot allocate gigabytes. Beyond this the middle of the page is one replace run.
MAX_CELLS = 600_000_000

# Unicode-word-boundary style: 1,099 and don't stay whole; other punctuation is its own token.
_TOKEN = re.compile(r"(\d+(?:[.,]\d+)+|\w+(?:['\u2019]\w+)*|[^\w\s])(\s*)")
_WORD = re.compile(r"\w+")
_HAS_WORD = re.compile(r"\w")
_NUMERIC_CELL = re.compile(r"[\s\d.,%$\u20ac\u00a3+-]*")
CELL_SEP = " | "
SEP = "\n"  # block separator token inside a replace run (blocks never contain newlines)


def count_words(text: str) -> int:
    return len(_WORD.findall(text))


@dataclass(slots=True)
class ChangedBlock:
    """A block of the new page that changed, with the character intervals that did. Keyword
    rules match against the whole ``text`` but only count matches that overlap a span."""

    text: str
    spans: list[tuple[int, int]]


@dataclass(slots=True)
class DiffResult:
    ops: list[dict[str, Any]]
    stats: dict[str, int]
    degraded: bool = False

    def to_json(self) -> dict[str, Any]:
        return {"ops": self.ops, "stats": self.stats, "degraded": self.degraded}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> DiffResult:
        return cls(data["ops"], data["stats"], bool(data.get("degraded", False)))

    @property
    def added_words(self) -> int:
        return self.stats["added_words"]

    @property
    def removed_words(self) -> int:
        return self.stats["removed_words"]

    @property
    def changed_words(self) -> int:
        return self.stats["changed_words"]

    @property
    def changed_blocks(self) -> int:
        return self.stats["changed_blocks"]

    @property
    def is_empty(self) -> bool:
        return all(op["t"] in ("eq", "mov") for op in self.ops)

    @property
    def has_additions(self) -> bool:
        """Something was added or modified (as opposed to only removed)."""
        for op in self.ops:
            if op["t"] == "ins":
                return True
            if op["t"] == "rep":
                tokens = op.get("tokens")
                if tokens is None or any(t[0] == "ins" for t in tokens):
                    return True
        return False

    def changed_regions(self, new_texts: Sequence[str]) -> list[str]:
        """The new-side text that changed, one string per changed block: whole inserted
        blocks, and for edited blocks the changed spans widened to whole words (so an edit
        of ``$1,299`` to ``$1,099`` yields ``$1,099``, never a bare ``099``). This is what
        keyword rules match against."""
        regions: list[str] = []
        for op in self.ops:
            if op["t"] == "ins":
                regions.extend(new_texts[op["new"][0] : op["new"][1] + 1])
            elif op["t"] == "rep":
                tokens = op.get("tokens")
                if tokens is None:
                    regions.extend(new_texts[op["new"][0] : op["new"][1] + 1])
                else:
                    regions.extend(_changed_spans(tokens))
        return [r for r in regions if r]

    def change_set(self, new_texts: Sequence[str]) -> list[ChangedBlock]:
        """Every inserted or edited block of the new page, with its changed character spans
        (the whole block for insertions and for edits too large for a word diff)."""
        out: list[ChangedBlock] = []

        def whole(j: int) -> ChangedBlock:
            return ChangedBlock(new_texts[j], [(0, len(new_texts[j]))])

        for op in self.ops:
            if op["t"] == "ins":
                out.extend(whole(j) for j in range(op["new"][0], op["new"][1] + 1))
            elif op["t"] == "rep":
                news = list(range(op["new"][0], op["new"][1] + 1))
                tokens = op.get("tokens")
                per_block = _block_spans(tokens) if tokens is not None else None
                if per_block is None or len(per_block) != len(news):
                    out.extend(whole(j) for j in news)
                    continue
                for j, (text, spans) in zip(news, per_block, strict=True):
                    # the reconstructed text must be the block's own, or offsets would lie
                    out.append(ChangedBlock(text, spans) if text == new_texts[j] else whole(j))
        return out

    def added_text(self, new_texts: Sequence[str]) -> str:
        return "\n".join(self.changed_regions(new_texts))

    def summary_text(self, new_texts: Sequence[str]) -> str:
        """Readable context for alerts: the full new text of every inserted or edited block."""
        parts: list[str] = []
        for op in self.ops:
            if op["t"] in ("ins", "rep"):
                parts.extend(new_texts[op["new"][0] : op["new"][1] + 1])
        return "\n".join(parts)

    def removed_regions(self, old_texts: Sequence[str]) -> list[str]:
        regions: list[str] = []
        for op in self.ops:
            if op["t"] == "del":
                regions.extend(old_texts[op["old"][0] : op["old"][1] + 1])
            elif op["t"] == "rep":
                tokens = op.get("tokens")
                if tokens is None:
                    regions.extend(old_texts[op["old"][0] : op["old"][1] + 1])
                else:
                    regions.extend(_changed_spans(tokens, "del"))
        return [r for r in regions if r]

    def removed_text(self, old_texts: Sequence[str]) -> str:
        return "\n".join(self.removed_regions(old_texts))


# -- tokens -----------------------------------------------------------------------------


def _widen(buf: str, spans: list[tuple[int, int]]) -> list[str]:
    """Grow each ``(start, end)`` span to the nearest whitespace boundaries; merge overlaps."""
    widened: list[list[int]] = []
    for a, b in spans:
        while a > 0 and not buf[a - 1].isspace():
            a -= 1
        while b < len(buf) and not buf[b].isspace():
            b += 1
        if widened and a <= widened[-1][1]:
            widened[-1][1] = max(widened[-1][1], b)
        else:
            widened.append([a, b])
    return [buf[a:b].strip() for a, b in widened if buf[a:b].strip()]


def _changed_spans(tokens: list[list[str]], side: str = "ins") -> list[str]:
    """Changed text of each block of a replace run, widened to whole words.

    ``side="ins"`` reads the new side (eq + ins tokens), ``"del"`` the old side."""
    other = "del" if side == "ins" else "ins"
    out: list[str] = []
    buf = ""
    spans: list[tuple[int, int]] = []
    for kind, text in tokens:
        if kind == other:
            continue
        for k, part in enumerate(text.split(SEP)):
            if k:
                out.extend(_widen(buf, spans))
                buf, spans = "", []
            if part:
                if kind == side and part.strip():
                    spans.append((len(buf), len(buf) + len(part.rstrip())))
                buf += part
    out.extend(_widen(buf, spans))
    return out


def _block_spans(tokens: list[list[str]]) -> list[tuple[str, list[tuple[int, int]]]]:
    """New-side text of each block of a replace run with the character spans of its inserted
    tokens (trailing whitespace excluded)."""
    out: list[tuple[str, list[tuple[int, int]]]] = []
    buf = ""
    spans: list[tuple[int, int]] = []
    for kind, text in tokens:
        if kind == "del":
            continue
        for k, part in enumerate(text.split(SEP)):
            if k:
                out.append((buf, spans))
                buf, spans = "", []
            if part:
                if kind == "ins" and part.strip():
                    spans.append((len(buf), len(buf) + len(part.rstrip())))
                buf += part
    out.append((buf, spans))
    return out


def tokenize(text: str, ignore_case: bool) -> list[tuple[str, str]]:
    """``(comparison key, display text)``; the display text keeps trailing whitespace."""
    out: list[tuple[str, str]] = []
    for m in _TOKEN.finditer(text):
        word = m.group(1)
        out.append((word.lower() if ignore_case else word, word + m.group(2)))
    return out


def _token_diff(
    old_runs: Sequence[str],
    new_runs: Sequence[str],
    ignore_case: bool,
    differ: Differ,
    max_tokens: int,
) -> tuple[list[list[str]], int, int, int] | None:
    """Word diff of two runs of blocks: ``(tokens, added, removed, changed)``, or ``None``
    when either side has more than ``max_tokens`` tokens (the caller stays at block level)."""
    # Every whitespace-separated chunk yields at least one token, so spaces + 1 is a cheap
    # lower bound: an oversized run is rejected without paying to tokenize it.
    if (
        max(sum(t.count(" ") + 1 for t in old_runs), sum(t.count(" ") + 1 for t in new_runs))
        > max_tokens
    ):
        return None
    pairwise = len(old_runs) == len(new_runs)
    groups: list[tuple[list[tuple[str, str]], list[tuple[str, str]]]] = []
    if pairwise:
        for o, n in zip(old_runs, new_runs, strict=True):
            groups.append((tokenize(o, ignore_case), tokenize(n, ignore_case)))
    else:
        old_t: list[tuple[str, str]] = []
        new_t: list[tuple[str, str]] = []
        for k, o in enumerate(old_runs):
            if k:
                old_t.append((SEP, SEP))
            old_t.extend(tokenize(o, ignore_case))
        for k, n in enumerate(new_runs):
            if k:
                new_t.append((SEP, SEP))
            new_t.extend(tokenize(n, ignore_case))
        groups.append((old_t, new_t))

    if max(sum(len(g[0]) for g in groups), sum(len(g[1]) for g in groups)) > max_tokens:
        return None
    out: list[list[str]] = []
    added = removed = changed = 0

    def emit(kind: str, text: str) -> None:
        if not text:
            return
        if out and out[-1][0] == kind:
            out[-1][1] += text
        else:
            out.append([kind, text])

    for gi, (ot, nt) in enumerate(groups):
        if gi:
            emit("eq", SEP)
        ok = [k for k, _ in ot]
        nk = [k for k, _ in nt]
        for tag, i1, i2, j1, j2 in differ.opcodes(ok, nk):
            if tag == "equal":
                emit("eq", "".join(t for _, t in nt[j1:j2]))
                continue
            dw = sum(1 for k, _ in ot[i1:i2] if _HAS_WORD.search(k))
            iw = sum(1 for k, _ in nt[j1:j2] if _HAS_WORD.search(k))
            removed += dw
            added += iw
            changed += max(dw, iw)
            emit("del", "".join(t for _, t in ot[i1:i2]))
            emit("ins", "".join(t for _, t in nt[j1:j2]))
    return out, added, removed, changed


# -- block alignment --------------------------------------------------------------------


def align(
    old_keys: Sequence[Hashable], new_keys: Sequence[Hashable], differ: Differ
) -> list[Opcode]:
    """Opcodes for two sequences, with the (cheap, common) shared prefix and suffix peeled
    off before the differ sees the middle."""
    n, m = len(old_keys), len(new_keys)
    pre = 0
    while pre < n and pre < m and old_keys[pre] == new_keys[pre]:
        pre += 1
    suf = 0
    while suf < n - pre and suf < m - pre and old_keys[n - 1 - suf] == new_keys[m - 1 - suf]:
        suf += 1
    ops: list[Opcode] = []
    if pre:
        ops.append(("equal", 0, pre, 0, pre))
    mid_old, mid_new = old_keys[pre : n - suf], new_keys[pre : m - suf]
    if mid_old or mid_new:
        if not mid_old:
            ops.append(("insert", pre, pre, pre, m - suf))
        elif not mid_new:
            ops.append(("delete", pre, n - suf, pre, pre))
        elif len(mid_old) * len(mid_new) > MAX_CELLS:
            ops.append(("replace", pre, n - suf, pre, m - suf))
        else:
            for tag, i1, i2, j1, j2 in differ.opcodes(mid_old, mid_new):
                ops.append((tag, i1 + pre, i2 + pre, j1 + pre, j2 + pre))
    if suf:
        ops.append(("equal", n - suf, n, m - suf, m))
    return ops


def _detect_moves(
    ops: list[Opcode], old_keys: Sequence[Hashable], new_keys: Sequence[Hashable]
) -> list[Opcode]:
    """Pair identical blocks deleted in one place and inserted in another as ``move`` ops."""
    deleted: dict[Hashable, list[int]] = {}
    for tag, i1, i2, _, _ in ops:
        if tag == "delete":
            for i in range(i1, i2):
                deleted.setdefault(old_keys[i], []).append(i)
    if not deleted:
        return ops
    for lst in deleted.values():
        lst.reverse()  # pop() takes the earliest
    pair: dict[int, int] = {}  # new index -> old index
    for tag, _, _, j1, j2 in ops:
        if tag == "insert":
            for j in range(j1, j2):
                src = deleted.get(new_keys[j])
                if src:
                    pair[j] = src.pop()
    if not pair:
        return ops
    moved_old = set(pair.values())
    out: list[Opcode] = []
    for tag, i1, i2, j1, j2 in ops:
        if tag == "delete":
            start = i1
            for i in range(i1, i2 + 1):
                if i == i2 or i in moved_old:
                    if i > start:
                        out.append(("delete", start, i, j1, j1))
                    start = i + 1
        elif tag == "insert":
            j = j1
            while j < j2:
                if j in pair:
                    k = j
                    while k + 1 < j2 and (k + 1) in pair and pair[k + 1] == pair[k] + 1:
                        k += 1
                    out.append(("move", pair[j], pair[k] + 1, j, k + 1))
                    j = k + 1
                else:
                    k = j
                    while k < j2 and k not in pair:
                        k += 1
                    out.append(("insert", i1, i1, j, k))
                    j = k
        else:
            out.append((tag, i1, i2, j1, j2))
    return out


# -- public entry -----------------------------------------------------------------------


def _table_cells(
    old_run: Sequence[str], new_run: Sequence[str], ignore_case: bool
) -> tuple[list[list[int]], list[bool]] | None:
    """Row pairs of a table: which cells changed, and whether each row's change is numeric only
    (so a viewer can highlight just the cell). ``None`` if the rows do not line up."""
    if len(old_run) != len(new_run):
        return None
    changed: list[list[int]] = []
    numeric: list[bool] = []
    for o, n in zip(old_run, new_run, strict=True):
        oc, nc = o.split(CELL_SEP), n.split(CELL_SEP)
        if len(oc) != len(nc):
            return None
        idx = [
            k
            for k, (a, b) in enumerate(zip(oc, nc, strict=True))
            if (a.lower() != b.lower() if ignore_case else a != b)
        ]
        changed.append(idx)
        numeric.append(
            bool(idx)
            and all(_NUMERIC_CELL.fullmatch(oc[k]) and _NUMERIC_CELL.fullmatch(nc[k]) for k in idx)
        )
    return changed, numeric


def diff_blocks(
    old: Sequence[str],
    new: Sequence[str],
    *,
    ignore_case: bool = True,
    detect_moves: bool = True,
    table: bool = False,
    differ: Differ | None = None,
    budget_s: float = BUDGET_S,
    max_blocks: int = MAX_BLOCKS,
    max_tokens: int = MAX_TOKENS,
    monotonic: Callable[[], float] = time.monotonic,
) -> DiffResult:
    """Diff two lists of block texts (``old`` -> ``new``)."""
    differ = differ or get_differ()
    started = monotonic()
    old_keys = [t.lower() for t in old] if ignore_case else list(old)
    new_keys = [t.lower() for t in new] if ignore_case else list(new)
    degraded = len(old) > max_blocks or len(new) > max_blocks

    opcodes = align(old_keys, new_keys, differ)
    if detect_moves:
        opcodes = _detect_moves(opcodes, old_keys, new_keys)

    ops: list[dict[str, Any]] = []
    added = removed = changed = blocks = 0
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            ops.append({"t": "eq", "old": [i1, i2 - 1], "new": [j1, j2 - 1]})
        elif tag == "insert":
            ops.append({"t": "ins", "new": [j1, j2 - 1]})
            w = sum(count_words(t) for t in new[j1:j2])
            added += w
            changed += w
            blocks += j2 - j1
        elif tag == "delete":
            ops.append({"t": "del", "old": [i1, i2 - 1]})
            w = sum(count_words(t) for t in old[i1:i2])
            removed += w
            changed += w
            blocks += i2 - i1
        elif tag == "move":
            ops.append({"t": "mov", "old": [i1, i2 - 1], "new": [j1, j2 - 1]})
        else:  # replace
            op: dict[str, Any] = {"t": "rep", "old": [i1, i2 - 1], "new": [j1, j2 - 1]}
            blocks += max(i2 - i1, j2 - j1)
            old_run, new_run = old[i1:i2], new[j1:j2]
            if not degraded and monotonic() - started > budget_s:
                degraded = True  # out of time: everything from here on stays at block level
            word_result = (
                None if degraded else _token_diff(old_run, new_run, ignore_case, differ, max_tokens)
            )
            if word_result is None:
                degraded = True  # oversized run, or over budget: block replacement only
                dw = sum(count_words(t) for t in old_run)
                iw = sum(count_words(t) for t in new_run)
                removed += dw
                added += iw
                changed += max(dw, iw)
            else:
                tokens, a, r, c = word_result
                op["tokens"] = tokens
                if table and (cells := _table_cells(old_run, new_run, ignore_case)):
                    op["cells"], op["numeric"] = cells
                added += a
                removed += r
                changed += c
            ops.append(op)
    return DiffResult(
        ops,
        {
            "added_words": added,
            "removed_words": removed,
            "changed_words": changed,
            "changed_blocks": blocks,
        },
        degraded,
    )
