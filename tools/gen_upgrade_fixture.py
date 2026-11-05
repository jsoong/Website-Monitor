"""Write ``tests/fixtures/upgrade/release-0.1.0.zip``: a data folder as the first release made it.

    uv run python tools/gen_upgrade_fixture.py

The zip is committed and NEVER regenerated once release 0.1.0 has shipped: it stands for data that
real users already have, which every later migration has to upgrade
(tests/packaging/test_upgrade.py).
So this refuses to run when the schema is no longer version 1, because it would then freeze
something other than release 0.1.0.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # the tests package

from tests.packaging.upgrade_support import RELEASE_1_FOLDER, populate, running  # noqa: E402
from tests.support.fixture_site import FixtureSite  # noqa: E402

from pagewatch.engine.paths import DataDir  # noqa: E402
from pagewatch.engine.store.db import latest_schema_version  # noqa: E402


async def build(out: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="pagewatch-fixture-") as tmp:
        dd = DataDir(Path(tmp) / "data").ensure()
        site = await FixtureSite().start()
        try:
            async with running(dd) as release:
                await populate(release, site)
        finally:
            await site.stop()
        conn = sqlite3.connect(dd.db_path)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        out.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(dd.db_path, "pagewatch.sqlite")  # not .db: git ignores *.db
            for blob in sorted(dd.blobs_dir.rglob("*")):
                if blob.is_file():
                    zf.write(blob, f"blobs/{blob.relative_to(dd.blobs_dir).as_posix()}")


def main() -> int:
    if latest_schema_version() != 1:
        print(
            f"the schema is at version {latest_schema_version()}, not 1: release 0.1.0's data "
            "cannot be regenerated from this code (and must not be: it is a frozen fixture)",
            file=sys.stderr,
        )
        return 1
    if RELEASE_1_FOLDER.exists():
        print(
            f"{RELEASE_1_FOLDER} exists; it is frozen. Delete it by hand to regenerate.",
            file=sys.stderr,
        )
        return 1
    asyncio.run(build(RELEASE_1_FOLDER))
    print(f"wrote {RELEASE_1_FOLDER} ({RELEASE_1_FOLDER.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
