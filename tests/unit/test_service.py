"""``pagewatch-cli service``: the Task Scheduler definition is checked here; registering it
with ``schtasks`` on a real Windows machine is a manual check."""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from pagewatch.cli import service
from pagewatch.cli.main import main as cli_main
from pagewatch.cli.service import CommandResult, ServiceError

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def parse(xml: str) -> ET.Element:
    # (the declaration says UTF-16; ElementTree wants the text without it)
    return ET.fromstring(xml.split("?>", 1)[1])


def find(root: ET.Element, path: str) -> ET.Element:
    node = root.find(path, NS)
    assert node is not None, path
    return node


def definition(**kw: str) -> ET.Element:
    return parse(service.build_task_xml(
        kw.get("program", r"C:\Python\pythonw.exe"),
        kw.get("arguments", "-m pagewatch.engine.main --supervised"),
        kw.get("user", r"HOME\jason"),
    ))  # fmt: skip


def test_the_task_starts_at_this_users_logon_and_runs_as_them_without_elevation() -> None:
    root = definition()
    assert find(root, "t:Triggers/t:LogonTrigger/t:UserId").text == r"HOME\jason"
    assert find(root, "t:Principals/t:Principal/t:UserId").text == r"HOME\jason"
    assert find(root, "t:Principals/t:Principal/t:LogonType").text == "InteractiveToken"
    assert find(root, "t:Principals/t:Principal/t:RunLevel").text == "LeastPrivilege"


def test_the_task_restarts_every_minute_has_no_time_limit_and_never_doubles_up() -> None:
    root = definition()
    assert find(root, "t:Settings/t:RestartOnFailure/t:Interval").text == "PT1M"
    assert int(find(root, "t:Settings/t:RestartOnFailure/t:Count").text or 0) >= 100
    assert find(root, "t:Settings/t:ExecutionTimeLimit").text == "PT0S"  # no run-time limit
    assert find(root, "t:Settings/t:MultipleInstancesPolicy").text == "IgnoreNew"


def test_the_task_runs_on_battery_and_keeps_running_when_unplugged() -> None:
    root = definition()
    assert find(root, "t:Settings/t:DisallowStartIfOnBatteries").text == "false"
    assert find(root, "t:Settings/t:StopIfGoingOnBatteries").text == "false"
    assert find(root, "t:Settings/t:RunOnlyIfIdle").text == "false"


def test_the_command_and_its_arguments_are_taken_literally_and_escaped() -> None:
    root = definition(program=r"C:\Program Files\Page & Watch\engine.exe",
                      arguments='--data-dir "C:\\Users\\a<b>\\PageWatch"', user="DOM\\o'neil&co")  # fmt: skip
    assert (
        find(root, "t:Actions/t:Exec/t:Command").text == r"C:\Program Files\Page & Watch\engine.exe"
    )
    assert (
        find(root, "t:Actions/t:Exec/t:Arguments").text == '--data-dir "C:\\Users\\a<b>\\PageWatch"'
    )
    assert find(root, "t:Triggers/t:LogonTrigger/t:UserId").text == "DOM\\o'neil&co"


def test_the_engine_is_started_supervised_so_it_does_not_respawn_itself() -> None:
    program, arguments = service.engine_command(Path(r"C:\data\PageWatch"))
    assert (
        program.lower().endswith(("python", "python3", "python.exe", "pythonw.exe"))
        or "python" in program
    )
    assert "-m pagewatch.engine.main" in arguments and "--supervised" in arguments
    assert "--data-dir" in arguments and "PageWatch" in arguments
    exe_program, exe_args = service.engine_command(None, exe=r"C:\PW\pagewatch-engine.exe")
    assert exe_program == r"C:\PW\pagewatch-engine.exe" and "-m" not in exe_args.split()
    assert "--supervised" in exe_args and "--data-dir" not in exe_args


@pytest.mark.skipif(sys.platform == "win32", reason="checks the non-Windows refusal")
def test_registering_the_task_needs_windows_and_says_so() -> None:
    with pytest.raises(ServiceError, match="only works on Windows"):
        service.install(None)
    with pytest.raises(ServiceError, match="only works on Windows"):
        service.uninstall()


def test_install_hands_schtasks_a_utf16_definition_and_reports_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    seen: dict[str, object] = {}

    def runner(argv: list[str]) -> CommandResult:
        seen["argv"] = argv
        path = Path(argv[argv.index("/XML") + 1])
        raw = path.read_bytes()
        seen["bom"] = raw[:2] in (b"\xff\xfe", b"\xfe\xff")
        seen["xml"] = raw.decode("utf-16")
        return CommandResult(0, "SUCCESS")

    message = service.install(Path(r"C:\data"), runner=runner, user=r"HOME\jason")
    argv = seen["argv"]
    assert isinstance(argv, list) and argv[:2] == ["schtasks", "/Create"]
    assert argv[argv.index("/TN") + 1] == service.TASK_NAME and "/F" in argv
    assert seen["bom"] is True and "<LogonTrigger>" in str(seen["xml"])
    assert "starts at logon" in message

    with pytest.raises(ServiceError, match="could not create"):
        service.install(None, runner=lambda argv: CommandResult(1, "Access is denied"))


def test_uninstall_and_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> CommandResult:
        calls.append(argv)
        return CommandResult(1, "ERROR: The system cannot find the file specified.")

    assert "not installed" in service.status(runner=runner)
    with pytest.raises(ServiceError, match="could not delete"):
        service.uninstall(runner=runner)
    assert calls[0][:2] == ["schtasks", "/Query"] and calls[1][:2] == ["schtasks", "/Delete"]
    assert service.status(runner=lambda argv: CommandResult(0, "Status: Ready")) == "Status: Ready"


# -- the command line ------------------------------------------------------------------------


def test_print_xml_works_anywhere_and_needs_no_engine(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli_main(["--data-dir", str(tmp_path / "pw"), "service", "install", "--print-xml"])
    out = capsys.readouterr().out
    assert code == 0 and "<RestartOnFailure>" in out and "--supervised" in out
    assert str(tmp_path / "pw") in out.replace("&quot;", '"')
    parse(out)  # well-formed


@pytest.mark.skipif(sys.platform == "win32", reason="checks the non-Windows refusal")
def test_install_off_windows_is_a_clear_error_not_a_crash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli_main(["--data-dir", str(tmp_path), "service", "install"])
    assert code == 1 and "only works on Windows" in capsys.readouterr().err
