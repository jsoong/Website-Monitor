import itertools
import random
from typing import Any

import pytest

from pagewatch.engine.pipeline.diff import DiffResult, align, diff_blocks, tokenize
from pagewatch.engine.pipeline.differs import available_differs, get_differ


def ops_of(res: DiffResult) -> list[str]:
    return [op["t"] for op in res.ops]


def test_identical_is_all_equal_and_empty() -> None:
    res = diff_blocks(["a", "b"], ["a", "b"])
    assert ops_of(res) == ["eq"] and res.is_empty and res.changed_words == 0 and not res.degraded


def test_insert_and_delete_blocks_with_inclusive_ranges() -> None:
    res = diff_blocks(["a", "b", "c"], ["a", "x y", "b", "c"])
    assert res.ops == [
        {"t": "eq", "old": [0, 0], "new": [0, 0]},
        {"t": "ins", "new": [1, 1]},
        {"t": "eq", "old": [1, 2], "new": [2, 3]},
    ]
    assert (res.added_words, res.removed_words, res.changed_blocks) == (2, 0, 1)
    res = diff_blocks(["a", "gone now", "c"], ["a", "c"])
    assert ops_of(res) == ["eq", "del", "eq"]
    assert (res.added_words, res.removed_words) == (0, 2)


def test_replace_gets_a_word_diff_like_the_spec_example() -> None:
    res = diff_blocks(["Intro", "Price $19 today"], ["Intro", "Price $17 today"])
    rep = next(op for op in res.ops if op["t"] == "rep")
    assert rep["tokens"] == [["eq", "Price $"], ["del", "19 "], ["ins", "17 "], ["eq", "today"]]
    assert (res.added_words, res.removed_words, res.changed_words, res.changed_blocks) == (
        1,
        1,
        1,
        1,
    )


def test_single_word_changed_counts_once_and_extra_words_count_fully() -> None:
    res = diff_blocks(["the quick fox"], ["the slow brown fox"])
    assert (res.added_words, res.removed_words, res.changed_words) == (2, 1, 2)


def test_pure_punctuation_change_is_a_change_but_zero_words() -> None:
    res = diff_blocks(["hello world"], ["hello, world"])
    assert res.changed_words == 0 and not res.is_empty and res.has_additions


def test_ignore_case_aligns_but_displays_original_case() -> None:
    assert diff_blocks(["Hello World"], ["hello world"], ignore_case=True).is_empty
    res = diff_blocks(["Hello World"], ["hello world"], ignore_case=False)
    assert not res.is_empty


def test_moved_block_is_a_mov_op_and_not_a_change_in_standard_mode() -> None:
    old, new = ["A one", "B two", "C three", "D four"], ["A one", "C three", "D four", "B two"]
    res = diff_blocks(old, new)
    assert "mov" in ops_of(res) and res.changed_words == 0 and res.changed_blocks == 0
    assert res.is_empty
    exact = diff_blocks(old, new, detect_moves=False)
    assert "mov" not in ops_of(exact) and exact.changed_words > 0


def test_move_plus_real_edit() -> None:
    old, new = ["A", "B b", "C", "D"], ["A", "C", "D", "B b", "new item here"]
    res = diff_blocks(old, new)
    assert "mov" in ops_of(res)
    assert res.added_words == 3 and res.removed_words == 0
    assert res.added_text(new) == "new item here"


def test_replace_with_different_block_counts_aligns_across_blocks() -> None:
    res = diff_blocks(["one two", "three four"], ["one two three", "four five"])
    rep = next(op for op in res.ops if op["t"] == "rep")
    assert any("\n" in t for _, t in rep["tokens"])  # block separators survive in the tokens
    assert res.added_words >= 1


def test_added_and_removed_text() -> None:
    old = ["keep", "old price $19", "bye"]
    new = ["keep", "new price $17", "hello there"]
    res = diff_blocks(old, new)
    added = res.added_text(new)
    assert "new" in added and "$17" in added and "hello there" in added and "keep" not in added
    removed = res.removed_text(old)
    assert "old" in removed and "$19" in removed


def test_has_additions_false_for_pure_removal() -> None:
    assert not diff_blocks(["a b c", "d"], ["a b c"]).has_additions
    assert not diff_blocks(["a b c d"], ["a b c"]).has_additions
    assert diff_blocks(["a b c"], ["a b c d"]).has_additions


def test_changed_regions_widen_to_whole_words_so_prices_stay_intact() -> None:
    old, new = ["RTX 4090 now $1,299 in stock"], ["RTX 4090 now $1,099 in stock"]
    res = diff_blocks(old, new)
    assert res.changed_regions(new) == ["$1,099"]
    # unrelated words in the same block are not part of the change
    assert "RTX" not in res.added_text(new)
    assert res.summary_text(new) == new[0]
    inserted = diff_blocks(["a"], ["a", "whole new block"])
    assert inserted.changed_regions(["a", "whole new block"]) == ["whole new block"]


def test_change_set_spans_cover_exactly_the_inserted_tokens() -> None:
    old = ["Price $19 per box", "static line"]
    new = ["Price $17 per box", "static line", "brand new block"]
    cs = diff_blocks(old, new).change_set(new)
    assert [(c.text, [c.text[a:b] for a, b in c.spans]) for c in cs] == [
        ("Price $17 per box", ["17"]),
        ("brand new block", ["brand new block"]),
    ]
    # moved blocks are not changes; degraded runs report whole blocks
    moved = diff_blocks(["a b", "c d", "e f"], ["c d", "e f", "a b"]).change_set(
        ["c d", "e f", "a b"]
    )
    assert moved == []
    big_old = [" ".join(f"o{i}_{j}" for j in range(50)) for i in range(3)]
    big_new = [" ".join(f"n{i}_{j}" for j in range(50)) for i in range(3)]
    degraded = diff_blocks(big_old, big_new, max_tokens=10).change_set(big_new)
    assert [c.spans for c in degraded] == [[(0, len(t))] for t in big_new]


def test_change_set_for_a_run_with_different_block_counts() -> None:
    old = ["alpha beta", "gamma delta"]
    new = ["alpha beta gamma", "delta epsilon"]
    cs = diff_blocks(old, new).change_set(new)
    assert [c.text for c in cs] == new and all(c.spans for c in cs)


def test_numbers_and_contractions_are_single_tokens() -> None:
    assert [k for k, _ in tokenize("Don't pay $1,299.50 or v2.0", True)] == [
        "don't",
        "pay",
        "$",
        "1,299.50",
        "or",
        "v2",
        ".",
        "0",
    ]


def test_json_roundtrip() -> None:
    res = diff_blocks(["a", "b"], ["a", "c"])
    assert DiffResult.from_json(res.to_json()).ops == res.ops


def test_tokenize_keeps_punctuation_and_trailing_space() -> None:
    assert tokenize("Price $19, ok", True) == [
        ("price", "Price "), ("$", "$"), ("19", "19"), (",", ", "), ("ok", "ok"),
    ]  # fmt: skip


# -- bounds -----------------------------------------------------------------------------


def test_oversized_replace_run_skips_word_diff_and_is_degraded() -> None:
    old = [" ".join(f"o{i}_{j}" for j in range(50)) for i in range(5)]
    new = [" ".join(f"n{i}_{j}" for j in range(50)) for i in range(5)]
    res = diff_blocks(old, new, max_tokens=100)
    rep = res.ops[0]
    assert rep["t"] == "rep" and "tokens" not in rep and res.degraded
    assert res.added_words == res.removed_words == 250  # still counted, from the blocks


def test_too_many_blocks_diffs_at_block_level_only() -> None:
    old = [f"old block {i}" for i in range(30)]
    new = [f"new block {i}" for i in range(30)]
    res = diff_blocks(old, new, max_blocks=10)
    assert res.degraded and "tokens" not in res.ops[0]


def test_time_budget_exhaustion_falls_back_to_block_level() -> None:
    ticks = itertools.count(0, 10)  # each monotonic() call is "10 s later"
    res = diff_blocks(
        ["a b", "x", "c d"], ["a q", "x", "c z"], budget_s=2.0, monotonic=lambda: next(ticks)
    )
    assert res.degraded
    assert all("tokens" not in op for op in res.ops if op["t"] == "rep")


# -- every differ gives a valid, equally-sized edit script ------------------------------


def reconstruct(res: DiffResult, old: list[str], new: list[str]) -> tuple[list[str], list[str]]:
    got_new: list[str] = []
    got_old: list[str] = []
    for op in res.ops:
        if op["t"] == "eq":
            got_new += old[op["old"][0] : op["old"][1] + 1]
            got_old += old[op["old"][0] : op["old"][1] + 1]
        elif op["t"] == "ins":
            got_new += new[op["new"][0] : op["new"][1] + 1]
        elif op["t"] == "del":
            got_old += old[op["old"][0] : op["old"][1] + 1]
        elif op["t"] == "rep" or op["t"] == "mov":
            got_new += new[op["new"][0] : op["new"][1] + 1]
            got_old += old[op["old"][0] : op["old"][1] + 1]
    return got_old, got_new


@pytest.mark.parametrize("name", available_differs())
def test_every_differ_reconstructs_both_sides(name: str) -> None:
    rng = random.Random(7)
    differ = get_differ(name)
    vocab = [f"block {i}" for i in range(12)]
    for _ in range(150):
        old = [rng.choice(vocab) for _ in range(rng.randint(0, 25))]
        new = list(old)
        for _ in range(rng.randint(0, 8)):
            action = rng.choice("idrm")
            if action == "i":
                new.insert(rng.randint(0, len(new)), rng.choice(vocab))
            elif action == "d" and new:
                del new[rng.randrange(len(new))]
            elif action == "r" and new:
                new[rng.randrange(len(new))] = rng.choice(vocab)
            elif action == "m" and len(new) > 1:
                new.insert(rng.randint(0, len(new) - 1), new.pop(rng.randrange(len(new))))
        res = diff_blocks(old, new, differ=differ, detect_moves=False, ignore_case=False)
        # without move detection, eq/ins/del/rep must reproduce both sequences exactly
        o, n = reconstruct(res, old, new)
        assert (o, n) == (old, new), (name, old, new, res.ops)
        res_m = diff_blocks(old, new, differ=differ, ignore_case=False)
        _, n2 = reconstruct(res_m, old, new)
        assert n2 == new


@pytest.mark.parametrize("name", available_differs())
def test_every_differ_finds_a_minimal_script_on_simple_cases(name: str) -> None:
    differ = get_differ(name)
    ops = align(["a", "b", "c", "d"], ["a", "c", "d", "e"], differ)
    stats: dict[str, Any] = {"eq": 0, "chg": 0}
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            stats["eq"] += i2 - i1
        else:
            stats["chg"] += (i2 - i1) + (j2 - j1)
    assert stats == {"eq": 3, "chg": 2}


def test_full_page_rewrite_at_the_block_cap_stays_inside_the_2s_budget() -> None:
    import time

    rng = random.Random(5)

    def page(n: int) -> list[str]:
        return [" ".join(f"w{rng.randrange(4000)}" for _ in range(15)) for _ in range(n)]

    old, new = page(20_000), page(20_000)
    t = time.perf_counter()
    res = diff_blocks(old, new)
    assert time.perf_counter() - t < 2.0
    assert res.degraded  # the run is too large for a word diff, and says so
    assert res.ops[0]["t"] == "rep" and "tokens" not in res.ops[0]
    assert res.added_words > 0 and res.removed_words > 0


def test_alignment_matrix_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    from pagewatch.engine.pipeline import diff as diffmod

    monkeypatch.setattr(diffmod, "MAX_CELLS", 100)
    old = [f"a{i}" for i in range(20)]
    new = [f"b{i}" for i in range(20)]
    res = diff_blocks(["same"] + old + ["end"], ["same"] + new + ["end"])
    assert [op["t"] for op in res.ops] == [
        "eq",
        "rep",
        "eq",
    ]  # one replace run, no alignment attempted


def test_table_mode_reports_which_cells_changed_and_whether_the_change_is_numeric() -> None:
    old = ["Item | Price | Stock", "Tea | $4.50 | yes", "Cocoa | $6.00 | yes"]
    new = ["Item | Price | Stock", "Tea | $4.75 | yes", "Cocoa | $6.00 | no"]
    res = diff_blocks(old, new, table=True)
    rep = next(op for op in res.ops if op["t"] == "rep")
    assert rep["cells"] == [[1], [2]] and rep["numeric"] == [True, False]
    plain = diff_blocks(old, new)
    assert "cells" not in next(op for op in plain.ops if op["t"] == "rep")


def test_table_cells_omitted_when_rows_do_not_line_up() -> None:
    res = diff_blocks(["a | b"], ["a | b | c"], table=True)
    assert "cells" not in next(op for op in res.ops if op["t"] == "rep")


# -- text rendering -----------------------------------------------------------------------


def test_render_marks_all_op_kinds_and_context_trimming() -> None:
    from pagewatch.engine.pipeline.render import render_marks

    ctx = [f"ctx{i}" for i in range(10)]
    old = ["h", "gone block", *ctx, "tail x"]
    new = ["h", *ctx, "tail y", "new block"]
    res = diff_blocks(old, new)
    lines = render_marks(res, old, new)
    assert lines[:2] == ["  h", "- gone block"]
    assert (
        "+ new block" in lines
        and "~ [-tail x-]{+tail y+}" in lines
        or any(ln.startswith("~ tail") for ln in lines)
    )
    trimmed = render_marks(res, old, new, context=2)
    assert "  ..." in trimmed and len(trimmed) < len(lines)
    moved_old, moved_new = ["a b", "c d", "e f"], ["c d", "e f", "a b"]
    assert "> a b" in render_marks(diff_blocks(moved_old, moved_new), moved_old, moved_new)
    # a degraded run (no word diff) shows the old block deleted and the new one inserted
    big_old, big_new = ["x y z"], ["p q r"]
    degraded = diff_blocks(big_old, big_new, max_tokens=1)
    assert render_marks(degraded, big_old, big_new) == ["- x y z", "+ p q r"]


def test_render_marks_keeps_a_space_after_a_bare_deletion_but_not_after_a_replacement() -> None:
    from pagewatch.engine.pipeline.render import render_marks

    assert render_marks(
        diff_blocks(["a big cat sat"], ["a cat sat"]), ["a big cat sat"], ["a cat sat"]
    ) == ["~ a [-big-] cat sat"]
    assert render_marks(
        diff_blocks(["a big cat"], ["a red cat"]), ["a big cat"], ["a red cat"]
    ) == ["~ a [-big-]{+red+} cat"]
