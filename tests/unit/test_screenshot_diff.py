"""Screenshot comparison (spec: Change detection and diffing → Screenshot comparison)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from pagewatch.engine.pipeline import screenshot as shot
from pagewatch.engine.pipeline.core import PipelineResult
from pagewatch.engine.store.blobs import BlobStore
from pagewatch.models import Rect
from tests.support.docs import page_png, png_bytes
from tests.support.pipeline_harness import Harness

BASE = [(100, 100, 500, 60, "black"), (100, 400, 600, 20, "gray")]


def region_tuples(diff: shot.ShotDiff) -> list[tuple[int, int, int, int]]:
    return [(r.x, r.y, r.w, r.h) for r in diff.regions]


def test_identical_pages_have_no_difference() -> None:
    a = page_png(BASE)
    d = shot.compare(a, a)
    assert d.identical and not d.significant and d.changed_pixels == 0 and d.regions == []
    assert d.ratio == 0.0 and d.height_change_pct == 0.0


def test_a_changed_area_is_boxed_and_counted() -> None:
    a = page_png(BASE)
    b = page_png([*BASE, (800, 600, 200, 100, "red")])
    d = shot.compare(a, b)
    assert d.significant and not d.identical and d.region_count == 1
    (x, y, w, h) = region_tuples(d)[0]
    # the box covers the changed rectangle (within the 8 px grid) and nothing else
    assert x <= 800 and y <= 600 and x + w >= 1000 and y + h >= 700
    assert x >= 792 and y >= 592 and x + w <= 1008 and y + h <= 708
    assert d.changed_pixels == pytest.approx(200 * 100, rel=0.02)
    assert d.ratio == pytest.approx(200 * 100 / (1366 * 900), rel=0.02)


def test_two_separate_changes_are_two_regions_ordered_largest_first() -> None:
    a = page_png()
    b = page_png([(50, 50, 100, 40, "black"), (900, 700, 300, 120, "black")])
    d = shot.compare(a, b)
    assert d.region_count == 2
    assert d.regions[0].w >= 296 and d.regions[1].w < 120  # the big one first


def test_nearby_changes_merge_into_one_region() -> None:
    a = page_png()
    b = page_png([(100, 100, 40, 12, "black"), (150, 100, 40, 12, "black")])  # 10 px apart
    assert shot.compare(a, b).region_count == 1


def test_a_difference_at_or_below_24_of_255_is_not_a_change_but_above_it_is() -> None:
    base = Image.new("L", (400, 300), 200)
    near, far = base.copy(), base.copy()
    near.paste(176, (0, 0, 400, 300))  # 24 darker: not marked
    far.paste(175, (0, 0, 400, 300))  # 25 darker: marked
    assert shot.compare(png_bytes(base), png_bytes(near)).changed_pixels == 0
    assert shot.compare(png_bytes(base), png_bytes(far)).changed_pixels == 400 * 300


def test_noise_below_min_ratio_is_not_significant_but_reported_as_a_difference() -> None:
    a = page_png()
    img = Image.open(io.BytesIO(a))
    img.putpixel((5, 5), (0, 0, 0))
    d = shot.compare(a, png_bytes(img))
    assert d.changed_pixels == 1 and not d.significant and not d.identical and d.region_count == 1
    assert shot.compare(
        a, png_bytes(img), min_ratio=0.0
    ).significant  # min_ratio 0: anything counts


def test_ratio_must_exceed_min_ratio() -> None:
    a = page_png()
    b = page_png([(0, 0, 1366, 5, "black")])  # 5 of 900 rows = 0.56 %
    assert shot.compare(a, b, min_ratio=0.002).significant
    assert not shot.compare(a, b, min_ratio=0.01).significant


def test_ignore_rectangles_are_blanked_on_both_sides() -> None:
    a = page_png(BASE)
    b = page_png([*BASE, (800, 600, 200, 100, "red")])
    d = shot.compare(a, b, ignore=[Rect(x=780, y=580, w=260, h=160)])
    assert d.identical and not d.significant
    partial = shot.compare(a, b, ignore=[Rect(x=780, y=580, w=60, h=60)])
    assert partial.significant  # most of the changed area is outside the rectangle


def test_height_change_over_5_percent_is_a_change_even_when_the_pixels_match() -> None:
    a = page_png(BASE, size=(1366, 1000))
    taller = page_png(BASE, size=(1366, 1080))  # +8 %, extra rows are blank white
    d = shot.compare(a, taller)
    assert d.height_old == 1000 and d.height_new == 1080
    assert d.height_change_pct == pytest.approx(8.0) and d.significant
    assert d.changed_pixels == 0  # white padding equals white page
    slightly = page_png(BASE, size=(1366, 1040))  # +4 %
    assert not shot.compare(a, slightly).significant
    assert shot.compare(a, slightly, height_change_pct=3.0).significant


def test_content_in_the_added_height_counts_as_changed_pixels() -> None:
    a = page_png(BASE, size=(1366, 900))
    b = page_png([*BASE, (100, 950, 500, 40, "black")], size=(1366, 1000))
    d = shot.compare(a, b)
    assert d.significant and d.changed_pixels == 500 * 40 and d.region_count == 1


def test_different_widths_do_not_crash_and_count_as_a_change() -> None:
    d = shot.compare(page_png(size=(1000, 500), background="black"),
                     page_png(size=(1200, 500), background="black"))  # fmt: skip
    assert d.significant  # the added 200 px column is white against the black page


def test_a_full_rewrite_is_one_region_and_stays_fast() -> None:
    import time

    a, b = page_png(size=(1366, 9000)), page_png(size=(1366, 9000), background="black")
    t0 = time.perf_counter()
    d = shot.compare(a, b)
    assert time.perf_counter() - t0 < 5.0
    assert d.ratio == 1.0 and d.region_count == 1 and region_tuples(d) == [(0, 0, 1366, 9000)]


def test_regions_are_capped_and_the_total_is_kept() -> None:
    boxes = [(30 + (i % 20) * 60, 30 + (i // 20) * 60, 20, 20, "black") for i in range(80)]
    d = shot.compare(page_png(), page_png(boxes))
    assert d.region_count == 80 and len(d.regions) == shot.MAX_REGIONS


def test_summary_and_json_round_trip() -> None:
    d = shot.compare(page_png(), page_png([(0, 0, 683, 900, "black")], size=(1366, 900)))
    assert d.summary().startswith("Visual change: 50.0% of the page changed, 1 region")
    back = shot.ShotDiff.from_json(d.to_json())
    assert back.changed_pixels == d.changed_pixels and region_tuples(back) == region_tuples(d)
    assert back.significant and back.height_old == 900
    tall = shot.compare(page_png(size=(1366, 1000)), page_png(size=(1366, 1200)))
    assert "height 1000→1200px" in tall.summary()


def test_overlay_draws_red_boxes_and_blue_ignore_rectangles() -> None:
    a = page_png(BASE)
    b = page_png([*BASE, (800, 600, 200, 100, "green")])
    ignore = [Rect(x=10, y=10, w=50, h=50)]
    d = shot.compare(a, b, ignore=ignore)
    img = Image.open(io.BytesIO(shot.overlay_png(b, d, ignore=ignore))).convert("RGB")
    assert img.size == (1366, 900)
    reg = d.regions[0]
    assert img.getpixel((reg.x - 1, reg.y + reg.h // 2)) == (255, 0, 0)  # the box edge
    assert img.getpixel((10, 30)) == (70, 110, 255)  # the ignore rectangle's outline
    assert img.getpixel((900, 650)) == (0, 128, 0)  # the page itself is untouched inside


def test_overlay_extends_to_the_old_height_when_the_page_shrank() -> None:
    old = page_png([(100, 900, 400, 50, "black")], size=(1366, 1000))
    new = page_png(size=(1366, 900))
    d = shot.compare(old, new)
    img = Image.open(io.BytesIO(shot.overlay_png(new, d, old_height=1000)))
    assert img.size == (1366, 1000)


# -- through the pipeline (the screenshot method) ---------------------------------------

HTML = "<html><body><h1>Dashboard</h1><p>All systems normal today</p></body></html>"


def run(h: Harness, png: bytes, html: str = HTML, **kw: object) -> PipelineResult:
    return h.run(html, png=png, **kw)


def test_the_first_screenshot_is_the_baseline_and_is_stored(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    res = run(h, page_png(BASE))
    assert res.kind == "first" and res.screenshot_hash and res.store_version
    assert BlobStore(tmp_path).get(res.screenshot_hash)[:8] == b"\x89PNG\r\n\x1a\n"


def test_identical_pixels_are_unchanged_even_when_the_markup_changed(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    run(h, page_png(BASE))
    res = run(h, page_png(BASE), HTML.replace("normal", "NORMAL AND DIFFERENT"))
    assert res.kind == "unchanged_raw" and not res.store_version


def test_below_the_threshold_is_unchanged_and_stores_nothing(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    first = run(h, page_png(BASE))
    img = Image.open(io.BytesIO(page_png(BASE)))
    img.putpixel((3, 3), (0, 0, 0))
    res = run(h, png_bytes(img))
    assert res.kind == "unchanged" and not res.store_version and res.screenshot_hash is None
    assert any("below the threshold" in w for w in res.warnings)
    assert h.latest is not None and h.latest.version_id == 1 and first.kind == "first"


def test_a_visual_change_alerts_with_its_overlay_and_summary(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    run(h, page_png(BASE))
    changed = page_png([*BASE, (800, 600, 200, 100, "red")])
    res = run(h, changed)
    assert res.kind == "stored" and res.alert and res.store_version
    assert res.summary and res.summary.startswith("Visual change:")
    assert res.stats == {"changed_blocks": 1} and res.compared_with == 1
    store = BlobStore(tmp_path)
    assert res.diff_hash
    payload = store.get_json(res.diff_hash)
    assert payload["type"] == "screenshot" and payload["significant"] is True
    assert payload["old"] != payload["new"] == res.screenshot_hash
    assert store.get(payload["overlay"])[:4] == b"\x89PNG"
    assert payload["regions"] and payload["region_count"] == 1


def test_ignore_rectangles_and_min_ratio_come_from_the_filter_config(tmp_path: Path) -> None:
    flt = {"screenshot": {"ignore": [{"x": 780, "y": 580, "w": 260, "h": 160}]}}
    h = Harness(tmp_path, filter=flt)
    run(h, page_png(BASE))
    ignored = run(h, page_png([*BASE, (800, 600, 200, 100, "red")]))
    assert (
        ignored.kind == "unchanged" and not ignored.store_version
    )  # differs, but only in the rectangle
    h2 = Harness(tmp_path / "b", filter={"screenshot": {"min_ratio": 0.5}})
    run(h2, page_png(BASE))
    assert run(h2, page_png([*BASE, (800, 600, 200, 100, "red")])).kind == "unchanged"


def test_text_rules_do_not_apply_but_bad_fetch_rules_still_do(tmp_path: Path) -> None:
    h = Harness(tmp_path, gate={"keywords": "never-present-word", "min_changed_words": 500,
                                "blacklist": ["service unavailable"]})  # fmt: skip
    run(h, page_png(BASE))
    res = run(h, page_png([*BASE, (800, 600, 200, 100, "red")]))
    assert res.alert  # keyword and threshold rules are text rules: not consulted
    bad = run(h, page_png([(0, 0, 1366, 900, "black")]), "<p>Service Unavailable</p>")
    assert bad.kind == "rejected" and bad.reason == "blacklist"


def test_switching_a_bookmark_to_screenshots_stores_a_silent_baseline(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    assert h.run(HTML).kind == "first"  # a text-only first version
    res = run(h, page_png(BASE))
    assert res.kind == "stored" and not res.alert and res.reason == "screenshot_baseline"
    assert res.screenshot_hash and h.latest is not None and h.latest.screenshot_hash
    assert run(h, page_png([*BASE, (1, 1, 800, 800, "black")])).alert  # compared from here on


def test_a_height_change_alerts(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    run(h, page_png(BASE, size=(1366, 1000)))
    res = run(h, page_png(BASE, size=(1366, 1200)))
    assert res.alert and res.summary and "height 1000→1200px" in res.summary
