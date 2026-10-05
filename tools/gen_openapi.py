"""Write docs/openapi.json from the engine's FastAPI app (no engine needs to be running).

Usage: uv run python tools/gen_openapi.py [--check]
``--check`` exits non-zero if the committed file is out of date (CI uses the unit test).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, cast

from pagewatch.engine.api.app import create_app
from pagewatch.engine.core import Engine

TARGET = Path(__file__).resolve().parent.parent / "docs" / "openapi.json"


def schema() -> dict[str, Any]:
    app = create_app(cast(Engine, None))  # the schema does not touch the engine
    out: dict[str, Any] = app.openapi()
    return out


def render() -> str:
    return json.dumps(schema(), indent=1, sort_keys=True, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    text = render()
    if "--check" in sys.argv:
        raise SystemExit(0 if TARGET.read_text() == text else 1)
    TARGET.write_text(text)
    print(f"wrote {TARGET} ({len(text)} bytes)")
