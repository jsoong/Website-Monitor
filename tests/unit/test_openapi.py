import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_committed_openapi_schema_is_current() -> None:
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        import gen_openapi
    finally:
        sys.path.pop(0)
    committed = (ROOT / "docs" / "openapi.json").read_text()
    assert committed == gen_openapi.render(), (
        "docs/openapi.json is stale: run `uv run python tools/gen_openapi.py`"
    )


def test_every_spec_m1_endpoint_is_present() -> None:
    import json

    paths = json.loads((ROOT / "docs" / "openapi.json").read_text())["paths"]
    expected = {
        ("get", "/health"), ("get", "/folders"), ("post", "/folders"),
        ("patch", "/folders/{folder_id}"), ("delete", "/folders/{folder_id}"),
        ("get", "/bookmarks"), ("post", "/bookmarks"), ("get", "/bookmarks/{bookmark_id}"),
        ("patch", "/bookmarks/{bookmark_id}"), ("delete", "/bookmarks/{bookmark_id}"),
        ("post", "/bookmarks/bulk"), ("post", "/bookmarks/{bookmark_id}/check"),
        ("post", "/check"), ("get", "/bookmarks/{bookmark_id}/changes"),
        ("post", "/bookmarks/{bookmark_id}/read"), ("get", "/settings"), ("put", "/settings"),
        ("post", "/autowatch"),
    }  # fmt: skip
    present = {(m, p) for p, ops in paths.items() for m in ops}
    assert expected <= present, expected - present
