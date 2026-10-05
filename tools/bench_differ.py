"""M1 benchmark: pick the Differ implementation.

Usage:  uv run python tools/bench_differ.py [--repeat 5] [--json out.json]

Scenarios cover the common case (a few edits on a page), pages at scale, reorders, the
many-duplicate-blocks worst case for LCS algorithms, token-level runs, and a full-page
rewrite at the 20,000-block cap. For each differ it reports the median time of the raw
``opcodes`` call and of the whole two-stage ``diff_blocks`` (with its 2 s budget), plus the
size of the edit script (smaller is a better alignment).
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import time
from collections.abc import Callable
from pathlib import Path

from pagewatch.engine.pipeline.diff import MAX_BLOCKS, diff_blocks
from pagewatch.engine.pipeline.differs import Differ, available_differs, get_differ
from pagewatch.engine.pipeline.extract import extract_blocks, parse_html

CORPUS = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "sites"
WORDS = [f"w{i}" for i in range(4000)]


def sentence(rng: random.Random, n: int = 12) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(n))


def page(rng: random.Random, n: int) -> list[str]:
    return [sentence(rng, rng.randint(4, 30)) for _ in range(n)]


def edit(rng: random.Random, blocks: list[str], k: int) -> list[str]:
    out = list(blocks)
    for _ in range(k):
        i = rng.randrange(len(out))
        kind = rng.choice("rid")
        if kind == "r":
            out[i] = sentence(rng, rng.randint(4, 30))
        elif kind == "i":
            out.insert(i, sentence(rng))
        elif len(out) > 1:
            del out[i]
    return out


def scenarios() -> dict[str, tuple[list[str], list[str]]]:
    rng = random.Random(1234)
    out: dict[str, tuple[list[str], list[str]]] = {}
    base = page(rng, 800)
    out["typical: 800 blocks, 3 edits"] = (base, edit(rng, base, 3))
    base = page(rng, 5000)
    out["large: 5,000 blocks, 25 edits"] = (base, edit(rng, base, 25))
    moved = list(base)
    chunk = moved[1000:1500]
    del moved[1000:1500]
    moved[3000:3000] = chunk
    out["reorder: 5,000 blocks, 500-block section moved"] = (base, moved)
    dup_pool = [sentence(rng) for _ in range(40)]
    dup = [rng.choice(dup_pool) for _ in range(5000)]
    out["duplicates: 5,000 blocks from 40 distinct"] = (dup, edit(rng, dup, 40))
    out["rewrite: 5,000 blocks all different"] = (page(rng, 5000), page(rng, 5000))
    out[f"rewrite: {MAX_BLOCKS:,} blocks all different"] = (
        page(rng, MAX_BLOCKS),
        page(rng, MAX_BLOCKS),
    )
    half = page(rng, 10000)
    out["half rewrite: 10,000 blocks, 50% replaced"] = (
        half,
        [b if i % 2 else sentence(rng) for i, b in enumerate(half)],
    )
    for path in sorted(CORPUS.glob("*/old.html")):
        new = path.with_name("new.html")
        if new.exists():
            old_b = [b.text for b in extract_blocks(parse_html(path.read_text(encoding="utf-8")))]
            new_b = [b.text for b in extract_blocks(parse_html(new.read_text(encoding="utf-8")))]
            out[f"corpus: {path.parent.name}"] = (old_b, new_b)
    return out


def timed(fn: Callable[[], object], repeat: int) -> float:
    samples = []
    for _ in range(repeat):
        t = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - t)
    return statistics.median(samples)


def script_size(differ: Differ, a: list[str], b: list[str]) -> int:
    return sum(
        (i2 - i1) + (j2 - j1) for tag, i1, i2, j1, j2 in differ.opcodes(a, b) if tag != "equal"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--json")
    ap.add_argument("--timeout", type=float, default=60.0, help="skip a cell slower than this")
    args = ap.parse_args()
    names = available_differs()
    results: dict[str, dict[str, dict[str, float | int | None]]] = {}
    print(f"{'scenario':<52}" + "".join(f"{n:>26}" for n in names))
    print(f"{'':<52}" + "".join(f"{'opcodes / full (script)':>26}" for _ in names))
    for title, (a, b) in scenarios().items():
        row = f"{title:<52}"
        results[title] = {}
        for name in names:
            differ = get_differ(name)
            t0 = time.perf_counter()
            try:
                one = timed(lambda d=differ, x=a, y=b: d.opcodes(x, y), 1)
            except Exception as exc:
                row += f"{type(exc).__name__:>26}"
                results[title][name] = {"error": str(exc)}  # type: ignore[dict-item]
                continue
            if one > args.timeout:
                row += f"{'> timeout':>26}"
                results[title][name] = {"opcodes_s": None, "full_s": None, "script": None}
                continue
            ops_t = timed(lambda d=differ, x=a, y=b: d.opcodes(x, y), args.repeat)
            full_t = timed(lambda d=differ, x=a, y=b: diff_blocks(x, y, differ=d), args.repeat)
            size = script_size(differ, a, b)
            degraded = diff_blocks(a, b, differ=differ).degraded
            results[title][name] = {
                "opcodes_s": round(ops_t, 5),
                "full_s": round(full_t, 5),
                "script": size,
                "degraded": int(degraded),
            }
            mark = "*" if degraded else " "
            row += f"{ops_t * 1000:>8.1f}ms /{full_t * 1000:>8.1f}ms{mark}({size:>5})"
            _ = t0
        print(row)
    print("\n* = diff_blocks degraded (hit a bound).  (n) = size of the edit script.")
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
