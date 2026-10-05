import pytest
from pydantic import ValidationError

from pagewatch.models import (
    ActionsConfig,
    BookmarkIn,
    FilterRule,
    GateConfig,
    ScheduleConfig,
    Settings,
    apply_patch,
    deep_merge,
)


def test_interval_has_a_60_second_floor() -> None:
    assert ScheduleConfig(interval_s=60).interval_s == 60
    with pytest.raises(ValidationError):
        ScheduleConfig(interval_s=59)


def test_adaptive_bounds_validated() -> None:
    with pytest.raises(ValidationError):
        ScheduleConfig.model_validate({"adaptive": {"min_s": 900, "max_s": 100}})
    with pytest.raises(ValidationError):
        ScheduleConfig.model_validate({"adaptive": {"factor": 1.0}})


def test_times_mode_needs_times_and_validates_format() -> None:
    with pytest.raises(ValidationError):
        ScheduleConfig(mode="times")
    with pytest.raises(ValidationError):
        ScheduleConfig(mode="times", times=["25:00"])
    cfg = ScheduleConfig(mode="times", times=["17:30", "09:00", "09:00"])
    assert cfg.times == ["09:00", "17:30"]


def test_unknown_keys_are_rejected() -> None:
    with pytest.raises(ValidationError):
        GateConfig.model_validate({"min_chars": 1, "typo_field": 2})


def test_filter_rules_validate_their_required_fields() -> None:
    with pytest.raises(ValidationError):
        FilterRule(type="selector")
    with pytest.raises(ValidationError):
        FilterRule(type="between")
    with pytest.raises(ValidationError):
        FilterRule(type="text", pattern="(", pattern_kind="regex")
    assert FilterRule(type="between", start="Latest").end is None


def test_bare_list_actions_column_default_is_accepted() -> None:
    cfg = ActionsConfig.model_validate([{"type": "toast"}])
    assert [a.type for a in cfg.actions] == ["toast"]
    assert ActionsConfig.model_validate([]).actions == []


def test_bookmark_url_scheme_checked() -> None:
    assert BookmarkIn(url=" https://example.com ").url == "https://example.com"
    with pytest.raises(ValidationError):
        BookmarkIn(url="javascript:alert(1)")


def test_deep_merge_and_patch_semantics() -> None:
    base = {"a": 1, "nested": {"x": 1, "y": 2}, "list": [1, 2]}
    assert deep_merge(base, {"nested": {"y": 3}, "list": [9]}) == {
        "a": 1,
        "nested": {"x": 1, "y": 3},
        "list": [9],
    }
    assert base["nested"] == {"x": 1, "y": 2}  # inputs untouched
    # None removes an override so the value is inherited again
    assert apply_patch(
        {"a": 1, "nested": {"x": 1, "y": 2}}, {"a": None, "nested": {"x": None}}
    ) == {"nested": {"y": 2}}


def test_settings_defaults_match_the_spec() -> None:
    s = Settings()
    assert (s.static_pool, s.browser_pool) == (32, 3)
    assert (s.per_host_concurrency, s.per_host_min_gap_s) == (2, 2.0)
    assert s.startup_delay_s == 30.0
    assert s.keep_changed_versions == 20
    assert s.notify_on_first_check is False
