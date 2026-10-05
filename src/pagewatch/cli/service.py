"""``pagewatch-cli service install | uninstall | status``: autostart through Windows Task Scheduler.

A Windows Service would run in session 0, with no toasts, no per-user credential store and an
awkward browser profile, so the engine is started by a Task Scheduler task at the user's logon
instead (spec: Autostart and lifecycle). The task:

* triggers at *this user's* logon and runs as that user, without elevation;
* is restarted every minute if it fails (exit code 3 means "restart me"; a clean Quit, exit
  code 0, is not a failure and is not restarted);
* has no run-time limit, may start and keep running on battery, and never starts a second
  instance (the engine's own mutex backs that up).

Only ``schtasks`` can set the restart, battery and run-time options, so the task is registered
from an XML definition. Generating that XML is pure and is tested on every platform; running
``schtasks`` needs Windows and is a manual check.
"""

from __future__ import annotations

import getpass
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

TASK_NAME = "PageWatch Engine"
RESTART_INTERVAL = "PT1M"
RESTART_COUNT = 999  # the most Task Scheduler accepts

Runner = Callable[[list[str]], "CommandResult"]


@dataclass(slots=True)
class CommandResult:
    returncode: int
    output: str


class ServiceError(RuntimeError):
    pass


def current_user() -> str:
    domain = os.environ.get("USERDOMAIN") or os.environ.get("COMPUTERNAME") or ""
    user = os.environ.get("USERNAME") or getpass.getuser()
    return f"{domain}\\{user}" if domain else user


def engine_command(data_dir: Path | None, exe: str | None = None) -> tuple[str, str]:
    """``(program, arguments)`` that start a supervised engine. Without ``--exe`` this is the
    Python that is running the CLI (``pythonw`` where there is one, so no console window stays
    open); the packaged build passes its own ``pagewatch-engine.exe``."""
    parts: list[str] = []
    if exe:
        program = exe
    else:
        python = Path(sys.executable)
        windowless = python.with_name("pythonw.exe")
        program = str(windowless if windowless.exists() else python)
        parts += ["-m", "pagewatch.engine.main"]
    parts += ["--supervised", "--no-console-log"]
    if data_dir is not None:
        parts += ["--data-dir", str(data_dir)]
    return program, subprocess.list2cmdline(parts)


def build_task_xml(
    program: str, arguments: str, user: str, *, working_dir: str | None = None
) -> str:
    """The Task Scheduler (schema 1.2) definition."""
    who = escape(user)
    work = (
        f"\n      <WorkingDirectory>{escape(working_dir)}</WorkingDirectory>" if working_dir else ""
    )
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Starts the PageWatch engine at logon and restarts it if it stops.</Description>
    <URI>\\{escape(TASK_NAME)}</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{who}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{who}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>{RESTART_INTERVAL}</Interval>
      <Count>{RESTART_COUNT}</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(program)}</Command>
      <Arguments>{escape(arguments)}</Arguments>{work}
    </Exec>
  </Actions>
</Task>
"""


def run_command(argv: list[str]) -> CommandResult:  # pragma: no cover - runs schtasks
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        argv, capture_output=True, text=True, check=False, timeout=60
    )
    return CommandResult(done.returncode, (done.stdout + done.stderr).strip())


def require_windows() -> None:
    if sys.platform != "win32":
        raise ServiceError(
            "service install registers a Windows Task Scheduler task and only works on Windows "
            "(use --print-xml to see the task definition)"
        )


def install(
    data_dir: Path | None, *, exe: str | None = None, runner: Runner = run_command,
    user: str | None = None,
) -> str:  # fmt: skip
    require_windows()
    program, arguments = engine_command(data_dir, exe)
    xml = build_task_xml(program, arguments, user or current_user())
    with tempfile.TemporaryDirectory(prefix="pagewatch-task-") as scratch:
        path = Path(scratch) / "task.xml"
        path.write_text(xml, encoding="utf-16")  # schtasks wants UTF-16 (with a BOM)
        result = runner(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(path), "/F"])
    if result.returncode != 0:
        raise ServiceError(f"schtasks could not create the task: {result.output}")
    return f"installed the '{TASK_NAME}' task: the engine starts at logon and restarts if it stops"


def uninstall(*, runner: Runner = run_command) -> str:
    require_windows()
    result = runner(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
    if result.returncode != 0:
        raise ServiceError(f"schtasks could not delete the task: {result.output}")
    return f"removed the '{TASK_NAME}' task"


def status(*, runner: Runner = run_command) -> str:
    require_windows()
    result = runner(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST", "/V"])
    if result.returncode != 0:
        return f"the '{TASK_NAME}' task is not installed"
    return result.output
