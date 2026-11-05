"""Regenerate ``packaging/assets/pagewatch.ico``, the icon of the three programs, the installer
and the Start menu shortcuts.

    uv run python tools/make_icon.py            # writes packaging/assets/pagewatch.ico
    uv run python tools/make_icon.py out.ico    # somewhere else

It is the tray's page glyph (``pagewatch.engine.tray.make_icon``) on a rounded tile, so the
application and the tray icon read as one thing. The .ico is committed; the build never runs this.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

from pagewatch.engine.tray import TrayState, make_icon

SIZES = (16, 24, 32, 48, 64, 128, 256)
TILE = (31, 78, 140, 255)  # deep blue
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "packaging" / "assets" / "pagewatch.ico"


def render(size: int = 256) -> Image.Image:
    """The icon at ``size`` px: white page glyph and status dot on a rounded blue tile."""
    tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(tile).rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=size * 0.22, fill=TILE
    )
    glyph = make_icon(TrayState.NORMAL, dark=True, size=int(size * 0.78))
    offset = (size - glyph.width) // 2
    tile.alpha_composite(glyph, (offset, offset))
    return tile


def write_ico(out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    render(256).save(out, format="ICO", sizes=[(s, s) for s in SIZES])
    return out


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    out = write_ico(Path(args[0]) if args else DEFAULT_OUT)
    print(f"wrote {out} ({out.stat().st_size} bytes, sizes {', '.join(map(str, SIZES))})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
