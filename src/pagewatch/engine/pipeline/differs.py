"""Pluggable sequence differs behind one interface.

Every implementation returns difflib-style opcodes ``(tag, i1, i2, j1, j2)`` with half-open
ranges and tags ``equal | delete | insert | replace``. Which one ships as the default was
chosen by ``tools/bench_differ.py`` on the golden corpus (see docs/DECISIONS.md).
"""

from __future__ import annotations

import difflib
import os
from collections.abc import Callable, Hashable, Sequence
from typing import Protocol

Opcode = tuple[str, int, int, int, int]


class Differ(Protocol):
    name: str

    def opcodes(self, a: Sequence[Hashable], b: Sequence[Hashable]) -> list[Opcode]: ...


def merge_changes(ops: Sequence[tuple[str, int, int, int, int]]) -> list[Opcode]:
    """Collapse adjacent non-equal ops into one ``replace``/``delete``/``insert`` and drop
    empty ones, so implementations that only emit insert/delete (Indel, DMP) match difflib."""
    out: list[Opcode] = []
    pending: list[int] | None = None  # [i1, i2, j1, j2]

    def flush() -> None:
        nonlocal pending
        if pending is not None:
            i1, i2, j1, j2 = pending
            if i2 > i1 and j2 > j1:
                out.append(("replace", i1, i2, j1, j2))
            elif i2 > i1:
                out.append(("delete", i1, i2, j1, j2))
            elif j2 > j1:
                out.append(("insert", i1, i2, j1, j2))
            pending = None

    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            if i2 > i1:
                flush()
                out.append((tag, i1, i2, j1, j2))
        elif pending is None:
            pending = [i1, i2, j1, j2]
        else:
            pending[1], pending[3] = i2, j2
    flush()
    return out


class DifflibDiffer:
    name = "difflib"

    def opcodes(self, a: Sequence[Hashable], b: Sequence[Hashable]) -> list[Opcode]:
        return merge_changes(difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes())


class CDifflibDiffer:
    name = "cdifflib"

    def __init__(self) -> None:
        from cdifflib import CSequenceMatcher

        self._matcher = CSequenceMatcher

    def opcodes(self, a: Sequence[Hashable], b: Sequence[Hashable]) -> list[Opcode]:
        return merge_changes(self._matcher(None, a, b, autojunk=False).get_opcodes())


class RapidfuzzDiffer:
    """Bit-parallel LCS (Indel distance) in C++: insert/delete opcodes, merged to replace."""

    name = "rapidfuzz"

    def __init__(self) -> None:
        from rapidfuzz.distance import Indel

        self._indel = Indel

    def opcodes(self, a: Sequence[Hashable], b: Sequence[Hashable]) -> list[Opcode]:
        return merge_changes(
            [
                (o.tag, o.src_start, o.src_end, o.dest_start, o.dest_end)
                for o in self._indel.opcodes(a, b)
            ]
        )


class DmpDiffer:
    """fast-diff-match-patch works on strings: map every distinct item to one character."""

    name = "dmp"

    def __init__(self) -> None:
        import fast_diff_match_patch

        self._diff = fast_diff_match_patch.diff

    def opcodes(self, a: Sequence[Hashable], b: Sequence[Hashable]) -> list[Opcode]:
        table: dict[Hashable, str] = {}

        def encode(seq: Sequence[Hashable]) -> str:
            chars: list[str] = []
            for item in seq:
                ch = table.get(item)
                if ch is None:
                    code = len(table) + 0x100
                    if code >= 0xD800:  # skip the surrogate block
                        code += 0x800
                    if code > 0x10FFFF:
                        raise ValueError("too many distinct items for the DMP differ")
                    ch = chr(code)
                    table[item] = ch
                chars.append(ch)
            return "".join(chars)

        ea, eb = encode(a), encode(b)
        ops: list[Opcode] = []
        i = j = 0
        for op, n in self._diff(ea, eb, timelimit=0, checklines=False, cleanup="No"):
            if op == "=":
                ops.append(("equal", i, i + n, j, j + n))
                i, j = i + n, j + n
            elif op == "-":
                ops.append(("delete", i, i + n, j, j))
                i += n
            else:
                ops.append(("insert", i, i, j, j + n))
                j += n
        return merge_changes(ops)


_FACTORIES: dict[str, Callable[[], Differ]] = {
    "difflib": DifflibDiffer,
    "cdifflib": CDifflibDiffer,
    "rapidfuzz": RapidfuzzDiffer,
    "dmp": DmpDiffer,
}

# Chosen by the M1 benchmark (tools/bench_differ.py); see docs/DECISIONS.md.
DEFAULT_DIFFER_NAME = "rapidfuzz"

_cache: dict[str, Differ] = {}


def available_differs() -> list[str]:
    return list(_FACTORIES)


def get_differ(name: str | None = None) -> Differ:
    chosen = name or os.environ.get("PAGEWATCH_DIFFER") or DEFAULT_DIFFER_NAME
    if chosen not in _cache:
        if chosen not in _FACTORIES:
            raise ValueError(f"unknown differ {chosen!r}; choose from {sorted(_FACTORIES)}")
        _cache[chosen] = _FACTORIES[chosen]()
    return _cache[chosen]
