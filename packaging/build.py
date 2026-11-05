"""Build PageWatch for distribution.

    uv run python packaging/build.py                  # dist/PageWatch/  (the one-folder build)
    uv run python packaging/build.py --no-ui          # engine and CLI only: fast, for checking
    uv run python packaging/build.py --smoke          # build, then run packaging/smoke.py on it
    uv run python packaging/build.py --portable       # also dist/PageWatch-<version>-portable.zip
    uv run python packaging/build.py --installer      # also the Inno Setup installer (Windows)

Run it from a checkout with ``uv sync --extra ui --group packaging`` (``packaging/build.ps1`` does
that on Windows). PyInstaller builds for the platform it runs on, so the Windows installer is built
on Windows; on Linux this script produces a Linux folder, which is what CI uses to check the
packaging without a Windows machine. See ``packaging/README.md``.

Every step is a small function so ``tests/packaging`` can check the command lines it would run.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from types import ModuleType

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def _load(name: str) -> ModuleType:
    """Load a sibling module by path: this folder is not a package (see bundle.py)."""
    spec = importlib.util.spec_from_file_location(f"pagewatch_{name}", HERE / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bundle = _load("bundle")

USER_GUIDE = ROOT / "docs" / "USER_GUIDE.md"
PORTABLE_DIR = HERE / "portable"
INSTALLER_SCRIPT = HERE / "installer.iss"
SPEC_FILE = HERE / "pagewatch.spec"

#: Where Inno Setup 6 installs: (environment variable holding the base folder, path below it).
ISCC_CANDIDATES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ProgramFiles(x86)", ("Inno Setup 6", "ISCC.exe")),
    ("ProgramFiles", ("Inno Setup 6", "ISCC.exe")),
    ("LOCALAPPDATA", ("Programs", "Inno Setup 6", "ISCC.exe")),
)


class BuildError(RuntimeError):
    pass


# -- steps (pure where possible) ----------------------------------------------------------


def pyinstaller_command(out: Path, work: Path, *, clean: bool) -> list[str]:
    cmd = [
        sys.executable, "-m", "PyInstaller", "--noconfirm",
        "--distpath", str(out), "--workpath", str(work),
    ]  # fmt: skip
    if clean:
        cmd.append("--clean")
    return [*cmd, str(SPEC_FILE)]


def find_iscc(env: dict[str, str] | None = None) -> Path | None:
    """The Inno Setup compiler: ``ISCC`` environment variable, ``PATH``, then the usual folders."""
    env = dict(os.environ) if env is None else env
    if env.get("ISCC") and Path(env["ISCC"]).is_file():
        return Path(env["ISCC"])
    for name in ("ISCC.exe", "ISCC"):
        on_path = shutil.which(name, path=env.get("PATH"))
        if on_path:
            return Path(on_path)
    for variable, parts in ISCC_CANDIDATES:
        base = env.get(variable)
        if base and (candidate := Path(base).joinpath(*parts)).is_file():
            return candidate
    return None


def iscc_command(iscc: Path, version: str, bundle_dir: Path, out: Path) -> list[str]:
    return [
        str(iscc), f"/DAppVersion={version}", f"/DSourceDir={bundle_dir}", f"/O{out}",
        str(INSTALLER_SCRIPT),
    ]  # fmt: skip


def installer_name(version: str) -> str:
    """Must equal ``OutputBaseFilename`` in installer.iss (a test pins this)."""
    return f"PageWatch-Setup-{version}.exe"


def portable_name(version: str) -> str:
    return f"PageWatch-{version}-portable.zip"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_checksum(path: Path) -> Path:
    """``<file>.sha256`` in the format ``sha256sum -c`` and PowerShell's Get-FileHash expect."""
    sidecar = path.with_name(path.name + ".sha256")
    sidecar.write_text(f"{sha256_of(path)} *{path.name}\n", encoding="ascii")
    return sidecar


def make_portable_zip(bundle_dir: Path, out_zip: Path, version: str) -> Path:
    """The one-folder build plus the portable launcher, zipped under one top-level folder.

    Portable mode is ``--data-dir``: the launcher points all three programs at ``data\\`` next
    to the programs, so the folder can be copied between machines or run beside another install.
    """
    top = f"PageWatch-{version}-portable"
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    if out_zip.exists():
        out_zip.unlink()
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(bundle_dir.rglob("*")):
            if path.is_file():
                zf.write(path, f"{top}/{path.relative_to(bundle_dir).as_posix()}")
        if PORTABLE_DIR.is_dir():
            for path in sorted(PORTABLE_DIR.iterdir()):
                if path.is_file():
                    zf.write(path, f"{top}/{path.name}")
    return out_zip


def run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
    print("$", " ".join(f'"{c}"' if " " in c else c for c in cmd), flush=True)
    done = subprocess.run(cmd, env=env, check=False)  # noqa: S603
    if done.returncode != 0:
        raise BuildError(f"{Path(cmd[0]).name} exited with code {done.returncode}")


# -- the build ----------------------------------------------------------------------------


def build(args: argparse.Namespace) -> list[Path]:
    version = bundle.read_version()
    out: Path = args.out.resolve()
    work: Path = args.work.resolve()
    bundle_dir = out / bundle.BUNDLE_NAME
    produced: list[Path] = []

    if args.installer and sys.platform != "win32":
        raise BuildError("--installer needs Windows (Inno Setup); build the folder here instead")
    iscc = None
    if args.installer:
        iscc = find_iscc()
        if iscc is None:
            raise BuildError(
                "Inno Setup 6 was not found. Install it (https://jrsoftware.org/isinfo.php, or "
                "`winget install JRSoftware.InnoSetup`) or set ISCC to ISCC.exe."
            )
    if not bundle.ICON.is_file():
        raise BuildError(f"{bundle.ICON} is missing; run `uv run python tools/make_icon.py`")

    env = dict(os.environ, PAGEWATCH_VERSION=version)
    if args.no_ui:
        env["PAGEWATCH_NO_UI"] = "1"
    print(f"building PageWatch {version} ({'without' if args.no_ui else 'with'} the UI)")
    run(pyinstaller_command(out, work, clean=args.clean), env=env)
    produced.append(bundle_dir)

    if USER_GUIDE.is_file():
        shutil.copy2(USER_GUIDE, bundle_dir / USER_GUIDE.name)

    if args.smoke:
        smoke = [sys.executable, str(HERE / "smoke.py"), str(bundle_dir),
                 "--expect-version", version]  # fmt: skip
        if not args.no_ui:
            smoke.append("--ui")
        run(smoke)

    if args.portable:
        zip_path = make_portable_zip(bundle_dir, out / portable_name(version), version)
        write_checksum(zip_path)
        produced.append(zip_path)

    if iscc is not None:
        run(iscc_command(iscc, version, bundle_dir, out))
        setup = out / installer_name(version)
        if not setup.is_file():
            raise BuildError(f"Inno Setup finished but {setup} does not exist")
        write_checksum(setup)
        produced.append(setup)
    return produced


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Build PageWatch for distribution")
    p.add_argument("--out", type=Path, default=ROOT / "dist", help="output folder (dist)")
    p.add_argument("--work", type=Path, default=ROOT / "build" / "pyinstaller")
    p.add_argument("--no-ui", action="store_true", help="leave out the UI and PySide6")
    p.add_argument("--clean", action="store_true", help="discard PyInstaller's cache first")
    p.add_argument("--smoke", action="store_true", help="run packaging/smoke.py on the result")
    p.add_argument("--portable", action="store_true", help="also make the portable zip")
    p.add_argument("--installer", action="store_true", help="also compile the installer (Windows)")
    args = p.parse_args(argv)
    try:
        produced = build(args)
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("\nproduced:")
    for path in produced:
        size = (
            sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
            if path.is_dir()
            else path.stat().st_size
        )
        print(f"  {path}  ({size / 1e6:.0f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
