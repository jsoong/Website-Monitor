"""The ``run_program`` action: start a program when a change passes the gate.

* **No shell.** The executable and its argument list go straight to the operating system, so
  nothing in a page can inject a command: a substituted value is always exactly one argument.
  Batch files (``.bat`` / ``.cmd``) are refused on Windows because starting one means running
  ``cmd.exe`` over the arguments, which brings the quoting problems back.
* **Variables** (spec): ``{url} {file_new} {file_old} {file_changes} {info1..3}`` plus ``{id}
  {name} {change_id} {datetime}``. The files are written to a private temporary folder only when
  an argument uses them, and removed when the program ends.
* **Timeout.** The process and everything it started are killed when it runs over.
* **Output** (stdout and stderr together, capped) is logged; a non-zero exit fails the job, which
  the queue retries and finally lists in Problems with the end of the output.
* Enabling it needs an explicit confirmation (``confirmed`` in the parameters).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import psutil

from pagewatch.engine.actions.base import ActionConfigError, AlertContext
from pagewatch.engine.actions.common import (
    detected_at,
    parse_params,
    template_values,
)
from pagewatch.engine.actions.content import ChangeContent, load_content
from pagewatch.engine.actions.templating import TemplateError, expand, variables_in
from pagewatch.engine.logs import get_logger
from pagewatch.models import RunProgramParams

if TYPE_CHECKING:
    from pagewatch.engine.core import Engine

log = get_logger("pagewatch.program")

OUTPUT_CAP = 64 * 1024
LOG_TAIL = 2000
ERROR_TAIL = 300
FILE_VARS = {"file_new", "file_old", "file_changes"}
CREATE_NO_WINDOW = 0x08000000


def extension_for(content_type: str, url: str) -> str:
    """A file extension that tells the receiving program what it was given."""
    ct = content_type.split(";")[0].strip().lower()
    table = {
        "text/html": ".html", "application/xhtml+xml": ".html", "application/json": ".json",
        "text/plain": ".txt", "application/pdf": ".pdf", "text/csv": ".csv", "text/xml": ".xml",
        "application/xml": ".xml", "application/rss+xml": ".xml", "application/atom+xml": ".xml",
        "image/png": ".png",
    }  # fmt: skip
    if ct in table:
        return table[ct]
    suffix = Path(url.split("?")[0]).suffix.lower()
    return suffix if 1 < len(suffix) <= 6 else ".bin"


def resolve_executable(name: str) -> str:
    candidate = name if os.path.isabs(name) else shutil.which(name)
    if not candidate or not os.path.isfile(candidate):
        raise ActionConfigError(f"run_program: {name!r} was not found")
    if not os.access(candidate, os.X_OK) and sys.platform != "win32":
        raise ActionConfigError(f"run_program: {name!r} is not executable")
    if sys.platform == "win32" and Path(candidate).suffix.lower() in (".bat", ".cmd"):
        raise ActionConfigError(
            "run_program: batch files are not supported (they run through cmd.exe, which "
            "re-interprets the arguments); start an .exe, or powershell/python with a script"
        )
    return candidate


async def _write_files(
    engine: Engine, ctx: AlertContext, wanted: set[str], folder: Path
) -> dict[str, str]:
    content: ChangeContent = await load_content(engine, ctx.change_id)
    ext = extension_for(content.content_type, ctx.url)
    files: dict[str, str] = {}

    async def put(var: str, name: str, data: bytes) -> None:
        path = folder / name
        await asyncio.to_thread(path.write_bytes, data)
        files[var] = str(path)

    if "file_new" in wanted:
        data = b""
        if content.new_raw_hash:
            data = await asyncio.to_thread(engine.blobs.get, content.new_raw_hash)
        await put("file_new", f"new{ext}", data)
    if "file_old" in wanted:
        data = b""
        if content.old_raw_hash:
            data = await asyncio.to_thread(engine.blobs.get, content.old_raw_hash)
        await put("file_old", f"old{ext}", data)
    if "file_changes" in wanted:
        await put("file_changes", "changes.txt", (content.result.changes_text + "\n").encode())
    return files


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(psutil.Error, ProcessLookupError):
        for child in psutil.Process(proc.pid).children(recursive=True):
            with contextlib.suppress(psutil.Error):
                child.kill()
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    await proc.wait()


async def _read_output(stream: asyncio.StreamReader | None) -> bytes:
    """Everything the program prints, keeping the first ``OUTPUT_CAP`` bytes (the rest is read
    and dropped so a chatty program never blocks on a full pipe)."""
    if stream is None:
        return b""
    kept = bytearray()
    while chunk := await stream.read(8192):
        if len(kept) < OUTPUT_CAP:
            kept += chunk[: OUTPUT_CAP - len(kept)]
    return bytes(kept)


async def run_process(
    argv: list[str], *, timeout_s: float, cwd: str | None, env: dict[str, str]
) -> tuple[int, str]:
    flags = CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, cwd=cwd, env=env, creationflags=flags,
        )  # fmt: skip
    except (FileNotFoundError, PermissionError, NotADirectoryError) as exc:
        raise ActionConfigError(f"run_program: could not start it: {exc}") from exc
    reader = asyncio.create_task(_read_output(proc.stdout))
    try:
        await asyncio.wait_for(proc.wait(), timeout_s)
    except TimeoutError:
        await _kill_tree(proc)
        reader.cancel()
        raise TimeoutError(
            f"run_program: {Path(argv[0]).name} did not finish within {timeout_s:g} s "
            "and was stopped"
        ) from None
    except asyncio.CancelledError:
        await _kill_tree(proc)
        reader.cancel()
        raise
    output = await reader
    assert proc.returncode is not None
    return proc.returncode, output.decode("utf-8", errors="replace")


async def run_program(engine: Engine, ctx: AlertContext) -> None:
    params = parse_params(RunProgramParams, ctx.params, "run_program")
    exe = resolve_executable(params.executable)
    used: set[str] = set().union(*(variables_in(a) for a in params.args)) if params.args else set()
    tmp_root = engine.data_dir.root / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix=f"run-{ctx.change_id}-", dir=tmp_root))
    try:
        values = template_values(ctx)
        if used & FILE_VARS:
            values |= await _write_files(engine, ctx, used & FILE_VARS, folder)
        try:
            args = [
                expand(a, values, when=detected_at(ctx), to_local=engine.zone.to_local)
                for a in params.args
            ]
        except TemplateError as exc:
            raise ActionConfigError(f"run_program argument: {exc}") from exc
        env = {
            **os.environ,
            "PAGEWATCH_BOOKMARK_ID": str(ctx.bookmark_id),
            "PAGEWATCH_CHANGE_ID": str(ctx.change_id),
        }
        code, output = await run_process(
            [exe, *args], timeout_s=params.timeout_s, cwd=params.cwd, env=env
        )
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    tail = output.strip()[-LOG_TAIL:]
    log.info(
        "program_finished", bookmark=ctx.bookmark_id, change=ctx.change_id,
        program=Path(exe).name, exit=code, output=tail,
    )  # fmt: skip
    if code != 0:
        raise RuntimeError(
            f"run_program: {Path(exe).name} exited with code {code}"
            + (f": {' '.join(tail[-ERROR_TAIL:].split())}" if tail else "")
        )
