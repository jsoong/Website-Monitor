"""Playwright codegen output -> macro steps. The action shapes are those of Playwright's own
recorder (``name`` plus ``selector`` / ``text`` / ``key`` / ``options`` / ``url``)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pagewatch.engine.logins.recorder import codegen_command, convert_codegen
from pagewatch.models import MacroDef

TYPED_PASSWORD = "Sup3r-Secret-Typed-In-Codegen"


def jsonl(*actions: dict[str, object]) -> str:
    header = {"browserName": "chromium", "launchOptions": {"headless": False}}
    return "\n".join(json.dumps(a) for a in (header, *actions))


SESSION = jsonl(
    {"name": "openPage", "url": "about:blank", "signals": []},
    {"name": "navigate", "url": "https://portal.example/login", "signals": []},
    {"name": "fill", "selector": "#username", "text": "alice", "signals": []},
    {"name": "fill", "selector": 'internal:label="Password"i', "text": TYPED_PASSWORD},
    {"name": "select", "selector": "#lang", "options": ["en"]},
    {"name": "check", "selector": "#remember"},
    {"name": "press", "selector": "#username", "key": "Tab", "modifiers": 0},
    {"name": "click", "selector": 'internal:role=button[name="Sign in"i]', "button": "left"},
    {"name": "click", "selector": "#menu", "button": "right"},
    {"name": "assertText", "selector": "h1", "text": "Account", "substring": True},
    {"name": "assertVisible", "selector": ".balance"},
    {"name": "setInputFiles", "selector": "#file", "files": ["a.txt"]},
    {"name": "closePage"},
)


def test_a_recorded_login_becomes_reviewable_steps() -> None:
    out = convert_codegen(SESSION, password_secret_key="portal-pw")
    assert [s.op for s in out.steps] == [
        "goto", "fill", "fill", "select", "click", "press", "click", "assert_text", "wait_for",
    ]  # fmt: skip
    goto, user, pw, select, remember, press, signin, assert_, visible = out.steps
    assert goto.url == "https://portal.example/login"  # type: ignore[union-attr]
    assert user.value == "alice"  # type: ignore[union-attr]
    assert pw.value.secret == "portal-pw"  # type: ignore[union-attr]
    assert select.value == "en"  # type: ignore[union-attr]
    assert signin.selector == 'internal:role=button[name="Sign in"i]'  # type: ignore[union-attr]
    assert assert_.text == "Account" and visible.selector == ".balance"  # type: ignore[union-attr]
    MacroDef.model_validate({"name": "recorded", "steps": [s.model_dump() for s in out.steps]})


def test_the_typed_password_is_never_kept() -> None:
    out = convert_codegen(SESSION, password_secret_key="portal-pw")
    everything = json.dumps([s.model_dump() for s in out.steps]) + " ".join(out.warnings)
    assert TYPED_PASSWORD not in everything
    assert any("portal-pw" in w and "NOT kept" in w for w in out.warnings)


def test_what_cannot_be_replayed_is_reported_not_silently_dropped() -> None:
    out = convert_codegen(SESSION)
    text = " ".join(out.warnings)
    assert "right or middle click" in text and "setInputFiles" in text
    assert "modifier" not in text  # modifiers: 0 means none


def test_modifier_keys_are_dropped_with_a_warning() -> None:
    out = convert_codegen(jsonl({"name": "press", "key": "a", "modifiers": 2}))
    assert out.steps[0].op == "press" and "modifier" in out.warnings[0]  # type: ignore[union-attr]


def test_repeated_navigation_is_collapsed_and_junk_lines_are_ignored() -> None:
    text = (
        jsonl(
            {"name": "navigate", "url": "https://a.example/"},
            {"name": "navigate", "url": "https://a.example/"},
        )
        + "\nnot json at all\n[1, 2]\n"
    )
    out = convert_codegen(text)
    assert [s.op for s in out.steps] == ["goto"]


def test_nothing_recorded_gives_no_steps() -> None:
    assert convert_codegen("").steps == []
    assert convert_codegen(jsonl({"name": "openPage", "url": "about:blank"})).steps == []


def test_the_recorder_runs_playwrights_bundled_node_driver_not_python_dash_m() -> None:
    """In the packaged build ``sys.executable`` is the engine itself, so ``python -m playwright``
    would start a second engine. The driver is what ``playwright install`` already uses there."""
    argv = codegen_command("msedge")("https://portal.example/login", Path("/tmp/rec/out.jsonl"))
    node, cli, *rest = argv
    assert Path(node).name in {"node", "node.exe"} and Path(cli).name == "cli.js"
    assert node != sys.executable and "-m" not in argv
    assert rest == [
        "codegen", "--target", "jsonl", "-o", str(Path("/tmp/rec/out.jsonl")),
        "--channel", "msedge", "https://portal.example/login",
    ]  # fmt: skip
    plain = codegen_command(None)("https://portal.example/", Path("/tmp/rec/out.jsonl"))
    assert "--channel" not in plain
