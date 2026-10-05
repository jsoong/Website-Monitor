from pagewatch.engine.pipeline import gate
from pagewatch.engine.pipeline.diff import diff_blocks
from pagewatch.models import GateConfig


def cfg(**kw: object) -> GateConfig:
    return GateConfig.model_validate(kw)


def test_bad_fetch_rules_in_order() -> None:
    texts = ["Service unavailable", "try again later"]
    assert gate.check_bad_fetch(cfg(), texts) is None
    v = gate.check_bad_fetch(cfg(min_chars=500), texts)
    assert v is not None and (v.reason, v.store, v.alert) == ("too_short", False, False)
    v = gate.check_bad_fetch(cfg(blacklist=["UNAVAILABLE"]), texts)
    assert v is not None and v.reason == "blacklist" and not v.store
    v = gate.check_bad_fetch(cfg(whitelist=["inventory"]), texts)
    assert v is not None and v.reason == "whitelist_miss"
    assert gate.check_bad_fetch(cfg(whitelist=["inventory", "later"]), texts) is None
    # too_short wins over blacklist (first failure wins)
    v = gate.check_bad_fetch(cfg(min_chars=500, blacklist=["unavailable"]), texts)
    assert v is not None and v.reason == "too_short"


def test_ignore_removed_content() -> None:
    removal = diff_blocks(["a b c", "d e"], ["a b c"])
    addition = diff_blocks(["a b c"], ["a b c", "d e"])
    assert (
        gate.check_change(cfg(ignore_removed=True), latest_diff=removal, anchor_diff=None).reason
        == "removed_only"
    )
    assert gate.check_change(cfg(ignore_removed=True), latest_diff=addition, anchor_diff=None).alert
    assert gate.check_change(
        cfg(), latest_diff=removal, anchor_diff=None
    ).alert  # off: removals alert


def test_per_check_threshold_compares_latest() -> None:
    small = diff_blocks(["x"], ["x", "one two"])
    big = diff_blocks(["x"], ["x", "one two three four five"])
    c = cfg(min_changed_words=4, threshold_mode="per_check")
    assert gate.check_change(c, latest_diff=small, anchor_diff=None).reason == "below_threshold"
    assert gate.check_change(c, latest_diff=big, anchor_diff=None).alert
    # per_check ignores the anchor entirely
    assert not gate.check_change(c, latest_diff=small, anchor_diff=big).alert
    assert not gate.uses_anchor(c)


def test_cumulative_threshold_compares_anchor() -> None:
    small = diff_blocks(["x"], ["x", "one two"])
    since_anchor = diff_blocks(["x"], ["x", "one two", "three four five"])
    c = cfg(min_changed_words=4, threshold_mode="cumulative")
    assert gate.uses_anchor(c)
    assert gate.check_change(c, latest_diff=small, anchor_diff=since_anchor).alert
    assert gate.check_change(c, latest_diff=small, anchor_diff=small).reason == "below_threshold"
    assert not gate.uses_anchor(cfg(threshold_mode="cumulative"))  # no threshold -> no anchor diff


def test_threshold_zero_means_off_and_ignore_removed_counts_additions_only() -> None:
    tiny = diff_blocks(["x"], ["x", "y"])
    assert gate.check_change(cfg(), latest_diff=tiny, anchor_diff=None).alert
    replace = diff_blocks(["a b c d e"], ["a b c d e", "q r"])
    assert gate.threshold_words(replace, cfg(ignore_removed=True)) == replace.added_words
