"""The plugin runtime: loading, hook isolation, the context API (no engine needed)."""

from __future__ import annotations

import http.server
import os
import threading
from collections.abc import Iterator
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest

from pagewatch import plugin_api
from pagewatch.engine import plugin_runtime as rt
from pagewatch.engine.plugin_runtime import ABSENT, PluginSpec, Session, load, session_of
from pagewatch.plugin_api import PluginError, Verdict

BOOKMARK = {
    "id": 7, "name": "Shop", "url": "https://shop.example.com/list", "folder_id": None,
    "source_type": "auto", "check_method": "auto", "info1": "min=10 max=99.5", "info2": None,
    "info3": None, "note": 'label="two words" other=x', "plugin": "p", "status": "ok",
}  # fmt: skip


def write(tmp_path: Path, body: str, name: str = "p") -> Path:
    path = tmp_path / f"{name}.py"
    path.write_text(dedent(body), encoding="utf-8")
    return path


def session(path: Path, state: dict[str, Any] | None = None, **bookmark: Any) -> Session:
    spec = PluginSpec(
        "p", str(path), {**BOOKMARK, **bookmark}, state or {}, "2026-01-05T12:00:00.000000Z"
    )
    return Session(spec)


# -- loading ----------------------------------------------------------------------------------


def test_the_hooks_a_plugin_defines_are_found(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        '''
        """Does a thing.

        More text.
        """
        def compare(ctx, old, new): return None
        def after_check(ctx, run): pass
        def helper(): pass
        before_check = "not callable"
        ''',
    )
    loaded = load(path)
    assert loaded.error is None and loaded.hooks == {"compare", "after_check"}
    assert loaded.description == "Does a thing."


def test_a_plugin_that_cannot_be_imported_is_reported_not_raised(tmp_path: Path) -> None:
    broken = load(write(tmp_path, "def oops(:\n", "syntax"))
    assert broken.module is None and broken.hooks == frozenset() and broken.error
    raising = load(write(tmp_path, "raise RuntimeError('boom')\n", "raises"))
    assert raising.error == "RuntimeError: boom"
    missing = load(tmp_path / "nope.py")
    assert missing.error and "cannot read" in missing.error
    s = session(write(tmp_path, "def oops(:\n", "again"))
    assert s.notes == ["plugin_error:load"] and s.call("compare", [], []) is ABSENT


def test_a_changed_file_is_reloaded_and_an_unchanged_one_is_not(tmp_path: Path) -> None:
    path = write(tmp_path, "def compare(ctx, old, new): return None\n")
    first = load(path)
    assert load(path) is first  # cached
    path.write_text("def after_check(ctx, run): pass\n", encoding="utf-8")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    second = load(path)
    assert second is not first and second.hooks == {"after_check"}


# -- calling hooks ----------------------------------------------------------------------------


def test_a_hook_that_raises_is_logged_and_treated_as_absent(tmp_path: Path) -> None:
    s = session(write(tmp_path, "def compare(ctx, old, new): raise ValueError('bad')\n"))
    assert s.compare(["a"], ["b"], {}) is None  # defers: the check carries on
    assert s.notes == ["plugin_error:compare"]
    assert any("compare raised ValueError: bad" in text for _, text in s.logs)


def test_a_slow_hook_leaves_a_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rt, "SLOW_HOOK_S", -1.0)  # everything is slow
    s = session(write(tmp_path, "def check_keywords(ctx, text): return True\n"))
    assert s.check_keywords("x") is True  # the answer still counts
    assert s.notes == ["plugin_slow:check_keywords"]


def test_a_coroutine_hook_is_refused(tmp_path: Path) -> None:
    s = session(write(tmp_path, "async def compare(ctx, old, new): return None\n"))
    assert s.call("compare", [], []) is ABSENT and s.notes == ["plugin_error:compare"]


@pytest.mark.parametrize(
    ("returns", "expected"),
    [
        ("None", None), ("True", (True, None)), ("False", (False, "plugin")),
        ("Verdict.change('why')", (True, "why")), ("Verdict.no_change('quiet')", (False, "quiet")),
        ("Verdict.no_change()", (False, "plugin")),
    ],
)  # fmt: skip
def test_compare_results(
    tmp_path: Path, returns: str, expected: tuple[bool, str | None] | None
) -> None:
    body = (
        f"from pagewatch.plugin_api import Verdict\ndef compare(ctx, old, new): return {returns}\n"
    )
    verdict = session(write(tmp_path, body)).compare(["a"], ["b"], {"added_words": 1})
    assert (None if verdict is None else (verdict.alert, verdict.reason)) == expected


def test_compare_sees_the_blocks_and_the_statistics(tmp_path: Path) -> None:
    body = """
    def compare(ctx, old, new):
        ctx.state['seen'] = [old, new, ctx.diff['added_words']]
    """
    s = session(write(tmp_path, body))
    assert s.compare(["a", "b"], ["a", "c"], {"added_words": 4}) is None
    assert s.ctx.state["seen"] == [["a", "b"], ["a", "c"], 4]


def test_a_bad_return_type_is_a_noted_mistake(tmp_path: Path) -> None:
    s = session(write(tmp_path, "def compare(ctx, old, new): return 'yes'\n"))
    assert s.compare([], [], {}) is None and s.notes == ["plugin_error:compare"]
    s2 = session(write(tmp_path, "def preprocess(ctx, html): return 42\n", "q"))
    assert s2.preprocess("<p>x</p>") == "<p>x</p>" and s2.notes == ["plugin_error:preprocess"]


def test_preprocess_and_check_keywords_defer_when_absent_or_none(tmp_path: Path) -> None:
    s = session(write(tmp_path, "def check_keywords(ctx, t): return None\n"))
    assert s.preprocess("<p>x</p>") == "<p>x</p>" and s.check_keywords("x") is None
    t = session(write(tmp_path, "def check_keywords(ctx, t): return 0\n", "z"))
    assert t.check_keywords("x") is False


def test_session_of_accepts_a_spec_dict_a_session_or_nothing(tmp_path: Path) -> None:
    path = write(tmp_path, "def compare(ctx, old, new): return None\n")
    live = session(path)
    assert session_of(live) is live and session_of(None) is None and session_of("x") is None
    spec = {"name": "p", "path": str(path), "bookmark": BOOKMARK, "state": {"a": 1}, "now": "",
            "user_agent": ""}  # fmt: skip
    built = session_of(spec)
    assert built is not None and built.ctx.state == {"a": 1} and built.has("compare")


# -- the context ------------------------------------------------------------------------------


def test_state_travels_and_reports_whether_it_changed(tmp_path: Path) -> None:
    s = session(write(tmp_path, "def after_check(ctx, run): ctx.state['n'] = ctx.state.get('n', 0) + 1\n"),
                state={"n": 1, "keep": [1, 2]})  # fmt: skip
    assert not s.state_changed() and s.outcome().state is None
    s.call("after_check", object())
    out = s.outcome()
    assert s.state_changed() and out.state == {"n": 2, "keep": [1, 2]}
    assert s.spec.state == {"n": 1, "keep": [1, 2]}  # the spec the caller passed is untouched


def test_options_come_from_the_info_fields_and_the_note(tmp_path: Path) -> None:
    ctx = session(write(tmp_path, "")).ctx
    assert ctx.options == {"min": "10", "max": "99.5", "label": "two words", "other": "x"}
    assert ctx.option("max") == "99.5" and ctx.option("nope", "d") == "d"
    first = session(write(tmp_path, "", "b"), info1="a=1", info2="a=2").ctx
    assert first.option("a") == "1"  # the earlier field wins


def test_set_property_is_limited_to_the_info_fields_note_and_name(tmp_path: Path) -> None:
    ctx = session(write(tmp_path, "")).ctx
    ctx.set_property("info2", "hello")
    ctx.set_property("note", None)
    ctx.set_property("name", "Renamed")
    assert ctx._props == {"info2": "hello", "note": None, "name": "Renamed"}
    for bad in ("url", "enabled", "plugin", "info4"):
        with pytest.raises(PluginError, match="cannot set"):
            ctx.set_property(bad, "x")
    with pytest.raises(PluginError, match="needs a name"):
        ctx.set_property("name", "")


def test_the_bookmark_view_is_read_only(tmp_path: Path) -> None:
    ctx = session(write(tmp_path, "")).ctx
    assert ctx.bookmark.name == "Shop" and ctx.bookmark.url.endswith("/list")
    with pytest.raises(AttributeError):
        ctx.bookmark.name = "x"  # type: ignore[misc]


def test_now_is_the_engines_clock(tmp_path: Path) -> None:
    now = session(write(tmp_path, "")).ctx.now()
    assert (now.year, now.month, now.day, now.hour) == (2026, 1, 5, 12) and now.tzinfo is not None


def test_helpers(tmp_path: Path) -> None:
    h = session(write(tmp_path, "")).ctx.helpers
    assert h.normalize("  a  b\n c ") == "a b c"
    assert h.numbers("was $1,299.00 now 1.099,50 or 1,5") == [1299.0, 1099.5, 1.5]
    root = h.parse_html("<div><p class='a'>One <b>two</b></p><p>Three</p></div>")
    assert [h.text_of(e) for e in h.select(root, "p")] == ["One two", "Three"]
    assert h.pattern("Updated * ago", "wildcard").search("updated 5 minutes ago")
    assert h.pattern(r"\d+ views").search("12 VIEWS")
    assert h.parse_html("   ") is None


def test_notify_is_collected_for_the_engine(tmp_path: Path) -> None:
    s = session(write(tmp_path, "def after_check(ctx, run): ctx.notify('Title', 'Body')\n"))
    s.call("after_check", None)
    assert s.outcome().alerts == [("Title", "Body")]


# -- http_get ---------------------------------------------------------------------------------


@pytest.fixture
def web() -> Iterator[str]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib name
            body = f"hello from {self.path} ua={self.headers.get('User-Agent')}".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            return None

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def test_http_get_is_rate_limited_and_capped(
    tmp_path: Path, web: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = session(write(tmp_path, "")).ctx
    ctx.user_agent = "PW-test"
    monkeypatch.setattr(plugin_api, "HTTP_GAP_S", 0.0)
    r = ctx.http_get(web + "/a")
    assert r.status == 200 and r.text == "hello from /a ua=PW-test"
    slept: list[float] = []
    monkeypatch.setattr(plugin_api, "HTTP_GAP_S", 5.0)
    monkeypatch.setattr(plugin_api.time, "sleep", slept.append)
    ctx.http_get(web + "/b")  # the same host again, straight away: it must wait
    assert slept and 0 < slept[0] <= 5.0
    monkeypatch.setattr(plugin_api, "HTTP_MAX_CALLS", 3)
    ctx.http_get(web + "/c")  # the third call of this hook is the last one allowed
    with pytest.raises(PluginError, match="at most 3"):
        ctx.http_get(web + "/d")
    with pytest.raises(PluginError, match="http\\(s\\) URL"):
        ctx.http_get("file:///etc/passwd")
    ctx._http_calls = 0
    with pytest.raises(PluginError, match="http_get"):
        ctx.http_get("http://127.0.0.1:9/")  # nothing listens there


def test_verdict_constructors() -> None:
    assert Verdict.change() == Verdict(True, None) and Verdict.no_change("x") == Verdict(False, "x")
