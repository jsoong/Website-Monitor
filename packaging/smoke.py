"""Smoke test of a built (or installed) PageWatch folder: run the real executables.

    python packaging/smoke.py dist/PageWatch
    python packaging/smoke.py "C:\\Users\\me\\AppData\\Local\\Programs\\PageWatch" --browser --ui

Standard library only, so it runs with any Python and drives the programs from outside, the way a
user's machine does. It exists because the failures of a packaged build are not the ones the unit
tests see: a library that reads its own metadata at import, a data file PyInstaller did not copy,
a worker process that re-runs the application. Each of those has happened while writing this.

Stages (each prints ``ok`` or ``FAIL``; the exit code is 0 only if all passed):

1. layout     the three programs, the Playwright driver, migrations, data files
2. versions   ``--version`` of the engine and the CLI agree (and match ``--expect-version``)
3. engine     starts on a throw-away data folder; ``/health`` needs the token
4. check      add a page through the CLI, check it (spawns the frozen worker pool), change the
              page, check again: exactly one change is recorded
5. browser    (``--browser``) a browser-method check through the bundled Playwright driver
6. ui         (``--ui``) the UI starts and stays up for a few seconds
7. stop       a clean stop: exit code 0 and the lockfile gone (POSIX; Windows has no console to
              signal, so the engine is terminated, which the crash-safety design must survive)
8. restart    the engine starts again on the same folder with all data intact

Nothing outside the temporary data folder is touched.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

IS_WINDOWS = sys.platform == "win32"
EXE_SUFFIX = ".exe" if IS_WINDOWS else ""
START_TIMEOUT_S = 90.0  # a cold start with antivirus scanning ~250 MB can be slow
CHECK_TIMEOUT_S = 90.0

PAGE_V1 = (
    "<html><head><title>Smoke page</title></head><body><h1>Smoke test</h1>"
    "<p>This is the first version of the page. It has enough words in it to count as real "
    "content rather than an empty page, which the engine would otherwise reject.</p></body></html>"
)
PAGE_V2 = (
    "<html><head><title>Smoke page</title></head><body><h1>Smoke test</h1>"
    "<p>A completely different second version follows. Several brand new sentences now appear "
    "here so the change is large enough for any default threshold, and the first paragraph has "
    "been replaced entirely.</p><p>Another new paragraph arrives with fresh wording as well.</p>"
    "</body></html>"
)


class SmokeFailure(Exception):
    pass


# -- helpers ------------------------------------------------------------------------------


def running_processes() -> str:
    """The command lines (Windows: image names) of every running process."""
    # -ww: never truncate to $COLUMNS, which cut a long install path before "QtWebEngineProcess"
    cmd = ["tasklist", "/FO", "CSV", "/NH"] if IS_WINDOWS else ["ps", "-eww", "-o", "args="]
    return subprocess.run(cmd, capture_output=True, text=True, check=False).stdout  # noqa: S603


def wait_until(
    what: str, condition: Callable[[], Any], timeout_s: float, interval_s: float = 0.25
) -> Any:
    deadline = time.monotonic() + timeout_s
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            value = condition()
            if value:
                return value
        except SmokeFailure:
            raise
        except Exception as exc:  # the thing is not up yet
            last = exc
        time.sleep(interval_s)
    detail = f" (last error: {last})" if last else ""
    raise SmokeFailure(f"timed out after {timeout_s:.0f}s waiting for {what}{detail}")


class PageServer:
    """A local web page whose content the test can change."""

    def __init__(self) -> None:
        self.body = PAGE_V1.encode()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(outer.body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(outer.body)

            def log_message(self, *_args: Any) -> None:
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/page.html"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> PageServer:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


class Engine:
    """One engine process on a data folder, and its API."""

    def __init__(self, exe: Path, data: Path, log_dir: Path, tag: str) -> None:
        self.exe, self.data, self.tag = exe, data, tag
        self.out = log_dir / f"engine-{tag}.out"
        self.proc: subprocess.Popen[bytes] | None = None
        self.port = 0
        self.token = ""

    def start(self) -> None:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0  # type: ignore[attr-defined,unused-ignore]
        with open(self.out, "wb") as sink:
            self.proc = subprocess.Popen(
                [str(self.exe), "--data-dir", str(self.data), "--no-tray", "--no-console-log"],
                stdout=sink, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                creationflags=flags,
            )  # fmt: skip
        lock = self.data / "engine.lock"

        def ready() -> bool:
            if self.proc is not None and self.proc.poll() is not None:
                raise SmokeFailure(
                    f"the engine exited with code {self.proc.returncode} while starting:\n"
                    f"{self.output_tail()}"
                )
            info = json.loads(lock.read_text(encoding="utf-8"))
            self.port, self.token = int(info["port"]), str(info["token"])
            return bool(self.api("GET", "/health"))

        wait_until(f"engine {self.tag} to answer /health", ready, START_TIMEOUT_S)

    def api(self, method: str, path: str, body: Any = None, *, token: str | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(  # noqa: S310 - loopback
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method
        )
        req.add_header("Authorization", f"Bearer {self.token if token is None else token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
            raw = resp.read()
        return json.loads(raw) if raw else None

    def output_tail(self, lines: int = 25) -> str:
        parts = []
        for path in (self.out, self.data / "logs" / "engine.log"):
            try:
                tail = path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
            except OSError:
                continue
            parts.append(f"--- {path.name} ---\n" + "\n".join(line[:300] for line in tail))
        return "\n".join(parts)

    def stop(self, *, graceful: bool) -> int | None:
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return None if proc is None else proc.returncode
        if graceful and not IS_WINDOWS:
            proc.send_signal(signal.SIGTERM)
        else:
            proc.terminate()
        try:
            return proc.wait(timeout=45)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=15)
            raise SmokeFailure("the engine did not stop within 45 s and was killed") from None


# -- the run ------------------------------------------------------------------------------


class Smoke:
    def __init__(self, dist: Path, args: argparse.Namespace, work: Path) -> None:
        self.dist = dist
        self.args = args
        self.work = work
        self.failures: list[str] = []
        self.internal = dist / "_internal" if (dist / "_internal").is_dir() else dist
        self.data = work / "data"
        self._engine: Engine | None = None
        self.bookmark_id = 0

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            raise SmokeFailure("the engine was never started")
        return self._engine

    def exe(self, name: str) -> Path:
        return self.dist / f"pagewatch-{name}{EXE_SUFFIX}"

    def cli(self, *argv: str, json_out: bool = False) -> Any:
        cmd = [str(self.exe("cli")), "--data-dir", str(self.data)]
        if json_out:
            cmd.append("--json")
        done = subprocess.run(  # noqa: S603
            [*cmd, *argv], capture_output=True, text=True, timeout=120, check=False
        )
        if done.returncode != 0:
            raise SmokeFailure(
                f"pagewatch-cli {' '.join(argv)} exited {done.returncode}: "
                f"{(done.stderr or done.stdout).strip()}"
            )
        return json.loads(done.stdout) if json_out else done.stdout

    def stage(self, name: str, fn: Callable[[], str | None]) -> bool:
        started = time.monotonic()
        try:
            detail = fn() or ""
        except SmokeFailure as exc:
            print(f"FAIL {name}: {exc}", flush=True)
            self.failures.append(name)
            return False
        except Exception as exc:
            print(f"FAIL {name}: {type(exc).__name__}: {exc}", flush=True)
            self.failures.append(name)
            return False
        took = time.monotonic() - started
        print(f"ok   {name}{': ' + detail if detail else ''} ({took:.1f}s)", flush=True)
        return True

    # stages ------------------------------------------------------------------------------

    def layout(self) -> str:
        missing: list[str] = []

        def need(path: Path, what: str) -> None:
            if not path.exists():
                missing.append(f"{what} ({path})")

        for name in ("engine", "cli"):
            need(self.exe(name), f"pagewatch-{name}")
        if self.args.ui:
            need(self.exe("ui"), "pagewatch-ui")
        driver = self.internal / "playwright" / "driver"
        node = driver / f"node{EXE_SUFFIX}"
        need(node, "Playwright's Node driver")
        need(driver / "package" / "cli.js", "Playwright's CLI")
        if node.exists() and not IS_WINDOWS and not os.access(node, os.X_OK):
            missing.append(f"Playwright's Node driver is not executable ({node})")
        migrations = self.internal / "pagewatch" / "engine" / "store" / "migrations"
        if not list(migrations.glob("0001_*.sql")):
            missing.append(f"SQL migrations ({migrations})")
        for name in ("volatile_patterns.yaml", "cookie_banner_selectors.txt"):
            need(self.internal / "pagewatch" / "data" / name, f"data/{name}")
        if IS_WINDOWS:  # Windows has no system time-zone database; schedules need tzdata
            need(self.internal / "tzdata" / "zoneinfo", "tzdata")
        if missing:
            raise SmokeFailure("missing from the bundle: " + "; ".join(missing))
        # The same program the engine runs to install the fallback Chromium: it must start.
        done = subprocess.run(  # noqa: S603
            [str(node), str(driver / "package" / "cli.js"), "--version"],
            capture_output=True, text=True, timeout=120, check=False,
        )  # fmt: skip
        if done.returncode != 0 or "Version" not in done.stdout:
            raise SmokeFailure(
                f"the bundled Playwright driver does not run (exit {done.returncode}): "
                f"{(done.stderr or done.stdout).strip()[:300]}"
            )
        size = sum(f.stat().st_size for f in self.dist.rglob("*") if f.is_file())
        return f"{size / 1e6:.0f} MB, Playwright driver {done.stdout.strip()}"

    def versions(self) -> str:
        found: dict[str, str] = {}
        for name in ("engine", "cli"):
            done = subprocess.run(  # noqa: S603
                [str(self.exe(name)), "--version"],
                capture_output=True, text=True, timeout=60, check=False,
            )  # fmt: skip
            text = (done.stdout + done.stderr).strip()
            if done.returncode != 0 or not text:
                raise SmokeFailure(
                    f"pagewatch-{name} --version gave exit {done.returncode}, output {text!r}"
                )
            found[name] = text.split()[-1]
        if len(set(found.values())) != 1:
            raise SmokeFailure(f"the programs disagree about the version: {found}")
        version = next(iter(found.values()))
        if self.args.expect_version and version != self.args.expect_version:
            raise SmokeFailure(f"version {version}, expected {self.args.expect_version}")
        return version

    def engine_up(self) -> str:
        self._engine = Engine(self.exe("engine"), self.data, self.work, "1")
        self._engine.start()
        try:
            self.engine.api("GET", "/health", token="wrong-token")
        except urllib.error.HTTPError as exc:
            if exc.code != 401:
                raise SmokeFailure(f"/health with a wrong token gave {exc.code}, not 401") from exc
        else:
            raise SmokeFailure("/health answered without the right token")
        health = self.engine.api("GET", "/health")
        lock = json.loads((self.data / "engine.lock").read_text(encoding="utf-8"))
        if health["pid"] != lock["pid"]:
            raise SmokeFailure(f"/health pid {health['pid']} != lockfile pid {lock['pid']}")
        return f"pid {health['pid']}, port {self.engine.port}, schema in {self.data.name}/"

    def runs(self, bookmark_id: int) -> list[dict[str, Any]]:
        page = self.engine.api("GET", f"/bookmarks/{bookmark_id}/runs")
        return [r for r in page["items"] if r["finished_at"]]

    def check_flow(self, page: PageServer) -> str:
        added = self.cli("add", page.url, "--name", "smoke", "--interval", "1m", json_out=True)
        bid = int(added["id"])
        self.bookmark_id = bid
        self.cli("check", str(bid))
        first = wait_until("the first check to finish", lambda: self.runs(bid), CHECK_TIMEOUT_S)
        outcomes = [r["outcome"] for r in first]
        if "first" not in outcomes or any(r["outcome"] == "error" for r in first):
            raise SmokeFailure(
                f"first check outcomes {outcomes}; log:\n{self.engine.output_tail()}"
            )
        page.body = PAGE_V2.encode()
        self.engine.api("POST", f"/bookmarks/{bid}/check?force=true")
        wait_until(
            "the changed page to be detected",
            lambda: any(r["outcome"] == "changed" for r in self.runs(bid)),
            CHECK_TIMEOUT_S,
        )
        changes = self.engine.api("GET", f"/bookmarks/{bid}/changes")["items"]
        if len(changes) != 1:
            raise SmokeFailure(
                f"expected exactly one change, found {len(changes)}; "
                f"runs {[r['outcome'] for r in self.runs(bid)]}"
            )
        runs = self.runs(bid)
        outcomes = [r["outcome"] for r in reversed(runs)]
        return f"outcomes {outcomes}, methods {sorted({r['method'] for r in runs})}"

    def browser_check(self, page: PageServer) -> str:
        added = self.cli(
            "add", page.url, "--name", "smoke-browser", "--method", "browser", json_out=True
        )
        bid = int(added["id"])
        self.engine.api("POST", f"/bookmarks/{bid}/check?force=true")
        runs = wait_until("the browser check to finish", lambda: self.runs(bid), 180)
        run = runs[0]
        if run["outcome"] == "error" or run["method"] != "browser":
            raise SmokeFailure(f"browser check: {run}\n{self.engine.output_tail()}")
        health = self.engine.api("GET", "/health")
        return (
            f"method {run['method']}, outcome {run['outcome']}, browser {health['browser_state']}"
        )

    def ui_starts(self) -> str:
        env = dict(os.environ)
        if not IS_WINDOWS:
            env.setdefault("QT_QPA_PLATFORM", "offscreen")
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                env.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox")
        sink = self.work / "ui.out"
        with open(sink, "wb") as out:
            proc = subprocess.Popen(  # noqa: S603
                [str(self.exe("ui")), "--data-dir", str(self.data)],
                stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
            )  # fmt: skip
        try:
            time.sleep(self.args.ui_seconds)
            if proc.poll() is not None:
                tail = sink.read_text(encoding="utf-8", errors="replace")[-1500:]
                raise SmokeFailure(f"the UI exited with code {proc.returncode}:\n{tail}")
            if "QtWebEngineProcess" not in running_processes():
                raise SmokeFailure(
                    "the UI is running but QtWebEngine's helper process never started, so the "
                    "change viewer would be blank: QtWebEngineProcess is missing from the bundle"
                )
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    proc.kill()
        return f"up for {self.args.ui_seconds:.0f}s with QtWebEngineProcess running"

    def stop_clean(self) -> str:
        code = self.engine.stop(graceful=True)
        if IS_WINDOWS:
            return f"terminated (exit code {code}); the restart stage checks recovery"
        if code != 0:
            raise SmokeFailure(f"the engine exited with code {code} on SIGTERM, not 0")
        if (self.data / "engine.lock").exists():
            raise SmokeFailure("engine.lock was left behind after a clean stop")
        return "exit code 0, lockfile removed"

    def restart(self) -> str:
        again = Engine(self.exe("engine"), self.data, self.work, "2")
        again.start()
        self._engine = again
        bid = self.bookmark_id
        bm = again.api("GET", f"/bookmarks/{bid}")
        changes = again.api("GET", f"/bookmarks/{bid}/changes")["items"]
        runs = self.runs(bid)
        if bm["name"] != "smoke" or len(changes) != 1 or len(runs) < 2:
            raise SmokeFailure(
                f"data changed across a restart: bookmark {bm['name']!r}, "
                f"{len(changes)} changes, {len(runs)} runs"
            )
        self.cli("status")
        return f"bookmark, {len(runs)} runs and {len(changes)} change intact"

    def run(self) -> int:
        print(f"smoke test of {self.dist}", flush=True)
        if not self.stage("layout", self.layout):
            return 1
        if not self.stage("versions", self.versions):
            return 1
        try:
            if not self.stage("engine", self.engine_up):
                return 1
            with PageServer() as page:
                self.stage("check", lambda: self.check_flow(page))
                if self.args.browser:
                    self.stage("browser", lambda: self.browser_check(page))
            if self.args.ui:
                self.stage("ui", self.ui_starts)
            self.stage("stop", self.stop_clean)
            if not self.failures:
                self.stage("restart", self.restart)
        finally:
            engine = self._engine
            if engine is not None and engine.proc is not None and engine.proc.poll() is None:
                engine.stop(graceful=True)
        if self.failures:
            print(f"\nFAILED: {', '.join(self.failures)}", flush=True)
            return 1
        print("\nall stages passed", flush=True)
        return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Smoke-test a built or installed PageWatch folder")
    p.add_argument("dist", type=Path, help="the folder with pagewatch-engine, -cli and _internal")
    p.add_argument("--browser", action="store_true", help="also run a browser-method check")
    p.add_argument("--ui", action="store_true", help="also start the UI and see it stay up")
    p.add_argument("--ui-seconds", type=float, default=10.0)
    p.add_argument("--expect-version", help="fail unless --version reports this")
    p.add_argument("--keep", action="store_true", help="keep the temporary data folder")
    args = p.parse_args(argv)
    dist = args.dist.resolve()
    work = Path(tempfile.mkdtemp(prefix="pagewatch-smoke-"))
    try:
        return Smoke(dist, args, work).run()
    finally:
        if args.keep:
            print(f"kept {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
