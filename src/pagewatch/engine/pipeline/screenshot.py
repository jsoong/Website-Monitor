"""Screenshot comparison (spec: Change detection and diffing → Screenshot comparison).

1. Load both PNGs and convert to grayscale (on a common canvas: the shorter one is padded with
   white, so content that appears or disappears counts as change).
2. Blank out the ignore rectangles on both.
3. Mark pixels whose absolute difference exceeds 24/255, dilate, and group into regions.
4. It is a change if the marked pixels exceed ``min_ratio`` of the page, or the page height
   changed by more than ``height_change_pct``.
5. An overlay PNG draws a red box around each region.

Only Pillow is used. Grouping runs on a coarse grid (the mask reduced by ``GRID``), where the
one-cell dilation also merges characters and lines into paragraph-sized regions, so a full-page
rewrite on a tall page stays cheap.
"""

from __future__ import annotations

import io
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter

THRESHOLD = 24  # of 255
GRID = 8  # pixels per coarse cell
MAX_REGIONS = 50  # reported and drawn (the largest first)
BOX_PAD = 2
MAX_GROUPED_CELLS = 60_000  # beyond this the page was rewritten: one region, no grouping
_LUT = [0] * (THRESHOLD + 1) + [255] * (255 - THRESHOLD)
Image.MAX_IMAGE_PIXELS = 120_000_000  # 1366 x 16384 full pages are ~22M; stay far below a bomb


@dataclass(slots=True)
class Region:
    x: int
    y: int
    w: int
    h: int
    cells: int = 0  # changed grid cells in the region (its weight)

    def to_json(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}


@dataclass(slots=True)
class ShotDiff:
    changed_pixels: int = 0
    total_pixels: int = 0
    ratio: float = 0.0
    regions: list[Region] = field(default_factory=list)
    region_count: int = 0  # before the MAX_REGIONS cap
    height_old: int = 0
    height_new: int = 0
    height_change_pct: float = 0.0
    significant: bool = False
    identical: bool = False  # not one pixel differs (before the threshold)

    def to_json(self) -> dict[str, Any]:
        return {
            "changed_pixels": self.changed_pixels,
            "total_pixels": self.total_pixels,
            "ratio": round(self.ratio, 6),
            "regions": [r.to_json() for r in self.regions],
            "region_count": self.region_count,
            "height_old": self.height_old,
            "height_new": self.height_new,
            "height_change_pct": round(self.height_change_pct, 3),
            "significant": self.significant,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> ShotDiff:
        return cls(
            changed_pixels=int(d.get("changed_pixels", 0)),
            total_pixels=int(d.get("total_pixels", 0)),
            ratio=float(d.get("ratio", 0.0)),
            regions=[Region(r["x"], r["y"], r["w"], r["h"]) for r in d.get("regions", [])],
            region_count=int(d.get("region_count", 0)),
            height_old=int(d.get("height_old", 0)),
            height_new=int(d.get("height_new", 0)),
            height_change_pct=float(d.get("height_change_pct", 0.0)),
            significant=bool(d.get("significant", False)),
        )

    def summary(self) -> str:
        n = self.region_count
        parts = [f"{self.ratio * 100:.1f}% of the page changed"]
        if n:
            parts.append(f"{n} region{'s' if n != 1 else ''}")
        if self.height_change_pct and self.height_old != self.height_new:
            parts.append(f"height {self.height_old}→{self.height_new}px")
        return "Visual change: " + ", ".join(parts)

    def stats(self) -> dict[str, int]:
        return {
            "changed_pixels": self.changed_pixels,
            "regions": self.region_count,
            "height_old": self.height_old,
            "height_new": self.height_new,
        }


RectLike = Any  # models.Rect or anything with x, y, w, h


def load_png(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


def _canvas(img: Image.Image, width: int, height: int, mode: str, fill: Any) -> Image.Image:
    if img.size == (width, height) and img.mode == mode:
        return img
    out = Image.new(mode, (width, height), fill)
    out.paste(img.convert(mode), (0, 0))
    return out


def _blank(img: Image.Image, rects: Iterable[RectLike]) -> None:
    draw = ImageDraw.Draw(img)
    for r in rects:
        draw.rectangle([r.x, r.y, r.x + r.w - 1, r.y + r.h - 1], fill=0)


def _components(coarse: Image.Image, dilated: Image.Image) -> list[Region]:
    """Group the dilated cells into 8-connected regions; each region's box covers the *changed*
    cells inside it (not the dilation halo). A rewrite of most of the page (more than
    ``MAX_GROUPED_CELLS`` cells) is one region: grouping it would cost seconds and say nothing."""
    cw, _ch = dilated.size
    base = coarse.tobytes()
    grown = [i for i, v in enumerate(dilated.tobytes()) if v]
    if len(grown) > MAX_GROUPED_CELLS:
        box = coarse.getbbox()
        if box is None:
            return []
        x0, y0, x1, y1 = box
        return [Region(x0 * GRID, y0 * GRID, (x1 - x0) * GRID, (y1 - y0) * GRID,
                       sum(1 for v in base if v))]  # fmt: skip
    parent = {i: i for i in grown}

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i in grown:
        x, y = i % cw, i // cw
        # neighbours already visited in raster order: left, up-left, up, up-right
        for dx, dy in ((-1, 0), (-1, -1), (0, -1), (1, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < cw and ny >= 0:
                j = ny * cw + nx
                if j in parent:
                    ra, rb = find(i), find(j)
                    if ra != rb:
                        parent[ra] = rb
    boxes: dict[int, list[int]] = {}
    for i in grown:
        if not base[i]:
            continue  # a halo cell: it joins regions but does not widen the box
        root = find(i)
        x, y = i % cw, i // cw
        b = boxes.get(root)
        if b is None:
            boxes[root] = [x, y, x, y, 1]
        else:
            b[0], b[1] = min(b[0], x), min(b[1], y)
            b[2], b[3] = max(b[2], x), max(b[3], y)
            b[4] += 1
    return [
        Region(x0 * GRID, y0 * GRID, (x1 - x0 + 1) * GRID, (y1 - y0 + 1) * GRID, n)
        for x0, y0, x1, y1, n in boxes.values()
    ]


def compare(
    old_png: bytes,
    new_png: bytes,
    *,
    ignore: Sequence[RectLike] = (),
    min_ratio: float = 0.002,
    height_change_pct: float = 5.0,
) -> ShotDiff:
    old_img, new_img = load_png(old_png), load_png(new_png)
    width = max(old_img.width, new_img.width)
    height = max(old_img.height, new_img.height)
    old = _canvas(old_img, width, height, "L", 255)
    new = _canvas(new_img, width, height, "L", 255)
    _blank(old, ignore)
    _blank(new, ignore)
    mask = ImageChops.difference(old, new).point(_LUT)
    changed = mask.histogram()[255]
    total = width * height
    out = ShotDiff(
        changed_pixels=changed,
        total_pixels=total,
        ratio=changed / total if total else 0.0,
        height_old=old_img.height,
        height_new=new_img.height,
        identical=changed == 0 and old_img.size == new_img.size,
    )
    tall = max(old_img.height, 1)
    out.height_change_pct = abs(new_img.height - old_img.height) / tall * 100.0
    if changed:
        coarse = mask.reduce(GRID).point([0] + [255] * 255)
        dilated = coarse.filter(ImageFilter.MaxFilter(3))
        regions = _components(coarse, dilated)
        regions.sort(key=lambda r: (-r.cells, r.y, r.x))
        out.region_count = len(regions)
        out.regions = [
            Region(r.x, r.y, min(r.w, width - r.x), min(r.h, height - r.y), r.cells)
            for r in regions[:MAX_REGIONS]
        ]
    out.significant = (
        changed > 0 and out.ratio > min_ratio
    ) or out.height_change_pct > height_change_pct
    return out


def overlay_png(
    new_png: bytes,
    diff: ShotDiff,
    *,
    ignore: Sequence[RectLike] = (),
    old_height: int | None = None,
) -> bytes:
    """The new screenshot with a red box around each changed region (and blue around the
    ignore rectangles). Drawn on the common canvas when the page height changed."""
    img = load_png(new_png)
    height = max(img.height, old_height or 0, diff.height_old)
    canvas = _canvas(img, img.width, height, "RGB", (255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    for r in ignore:
        draw.rectangle([r.x, r.y, r.x + r.w - 1, r.y + r.h - 1], outline=(70, 110, 255), width=2)
    for reg in diff.regions:
        draw.rectangle(
            [
                max(reg.x - BOX_PAD, 0),
                max(reg.y - BOX_PAD, 0),
                reg.x + reg.w + BOX_PAD,
                reg.y + reg.h + BOX_PAD,
            ],
            outline=(255, 0, 0),
            width=3,
        )
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()
