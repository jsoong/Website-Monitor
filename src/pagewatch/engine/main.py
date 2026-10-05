"""``pagewatch-engine``: the always-on process. It alone touches the network and writes data."""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import threading
from collections.abc import Sequence
from types import FrameType

from pagewatch import __version__
from pagewatch.engine.api.server import ApiServer
from pagewatch.engine.clock import iso
from pagewatch.engine.core import (
    EXIT_ALREADY_RUNNING,
    EXIT_ERROR,
    EXIT_OK,
    Engine,
)
from pagewatch.engine.instance import (
    InstanceGuard,
    read_lockfile,
    remove_lockfile,
    write_lockfile,
)
from pagewatch.engine.logs import configure_logging, get_logger
from pagewatch.engine.paths import DataDir, resolve_data_dir
from pagewatch.engine.store.db import SchemaTooNew
from pagewatch.models import LockInfo

log = get_logger("pagewatch.main")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pagewatch-engine", description="PageWatch engine")
    p.add_argument("--data-dir", help="data folder (default: %%LOCALAPPDATA%%\\PageWatch)")
    p.add_argument("--workers", choices=("process", "thread"), default="process")
    p.add_argument("--no-console-log", action="store_true", help="log to the file only")
    p.add_argument("--no-tray", action="store_true", help="do not create a tray icon")
    p.add_argument("--version", action="version", version=f"pagewatch-engine {__version__}")
    return p


async def run_engine(data_dir: DataDir, engine: Engine) -> int:
    await engine.start()
    api = ApiServer(engine)
    port = await api.start()
    started_at = iso(engine.clock.now())
    import os

    write_lockfile(
        data_dir,
        LockInfo(
            pid=os.getpid(),
            port=port,
            token=engine.token,
            version=__version__,
            started_at=started_at,
        ),
    )
    log.info("engine_listening", port=port)

    loop = asyncio.get_running_loop()

    def on_signal(signum: int, _frame: FrameType | None = None) -> None:
        log.info("signal_received", signal=signum)
        loop.call_soon_threadsafe(engine.request_stop, EXIT_OK)

    if threading.current_thread() is threading.main_thread():
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is not None:
                signal.signal(sig, on_signal)
    try:
        await engine.wait_stopped()
    finally:
        await api.stop()
        remove_lockfile(data_dir, only_pid=os.getpid())
        await engine.stop()
    return engine.exit_code


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    data_dir = resolve_data_dir(args.data_dir).ensure()
    guard = InstanceGuard(data_dir)
    if not guard.acquire():
        other = read_lockfile(data_dir)
        detail = f" (pid {other.pid}, port {other.port})" if other else ""
        print(
            f"PageWatch engine is already running for data folder {data_dir.root}{detail}.",
            file=sys.stderr,
        )
        return EXIT_ALREADY_RUNNING
    try:
        configure_logging(data_dir.logs_dir, console=not args.no_console_log)
        engine = Engine(data_dir, worker_mode=args.workers, enable_tray=not args.no_tray)
        return asyncio.run(run_engine(data_dir, engine))
    except SchemaTooNew as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        guard.release()


if __name__ == "__main__":
    raise SystemExit(main())
