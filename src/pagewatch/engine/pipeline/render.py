"""Rendering a diff for humans.

``render_marks`` is the plain-text view (always works, reused for "changes only" emails and
for golden-corpus snapshots):

    <two spaces>text            unchanged block
    + text                      inserted block
    - text                      deleted block
    > text                      moved block (shown at its new position)
    ~ Price $[-19-]{+17+} today edited block: [-removed-] {+added+}
"""

from __future__ import annotations

from collections.abc import Sequence

from pagewatch.engine.pipeline.diff import SEP, DiffResult


def _marked(tokens: list[list[str]]) -> list[str]:
    lines: list[str] = [""]
    for kind, text in tokens:
        for k, part in enumerate(text.split(SEP)):
            if k:
                lines.append("")
            if not part:
                continue
            if kind == "eq":
                lines[-1] += part
            elif kind == "del":
                lines[-1] += f"[-{part.rstrip()}-]" + part[len(part.rstrip()) :]
            else:
                lines[-1] += f"{{+{part.rstrip()}+}}" + part[len(part.rstrip()) :]
    return [ln.strip() for ln in lines if ln.strip()]


def render_marks(
    diff: DiffResult,
    old_texts: Sequence[str],
    new_texts: Sequence[str],
    *,
    context: int | None = None,
) -> list[str]:
    """Lines of the marked-up diff. ``context`` limits unchanged blocks shown around changes."""
    out: list[str] = []
    for op in diff.ops:
        t = op["t"]
        if t == "eq":
            block = new_texts[op["new"][0] : op["new"][1] + 1]
            if context is not None and len(block) > 2 * context:
                head, tail = block[:context], block[len(block) - context :]
                out += [f"  {x}" for x in head]
                out.append("  ...")
                out += [f"  {x}" for x in tail]
            else:
                out += [f"  {x}" for x in block]
        elif t == "ins":
            out += [f"+ {x}" for x in new_texts[op["new"][0] : op["new"][1] + 1]]
        elif t == "del":
            out += [f"- {x}" for x in old_texts[op["old"][0] : op["old"][1] + 1]]
        elif t == "mov":
            out += [f"> {x}" for x in new_texts[op["new"][0] : op["new"][1] + 1]]
        elif t == "rep":
            tokens = op.get("tokens")
            if tokens is None:
                out += [f"- {x}" for x in old_texts[op["old"][0] : op["old"][1] + 1]]
                out += [f"+ {x}" for x in new_texts[op["new"][0] : op["new"][1] + 1]]
            else:
                out += [f"~ {x}" for x in _marked(tokens)]
    return out
