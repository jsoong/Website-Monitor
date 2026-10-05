"""Global settings, stored one key per row in the ``setting`` table.

Only values the user changed are persisted; everything else comes from the model defaults
(or from ``overrides`` passed by tests, which sit *under* persisted values).
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from pagewatch.engine.store.db import Database
from pagewatch.models import Settings, deep_merge

STATE_PREFIX = "_state."  # engine bookkeeping in the `setting` table; never a user setting


class SettingsStore:
    def __init__(self, db: Database, overrides: dict[str, Any] | None = None) -> None:
        self._db = db
        self._overrides = overrides or {}
        self.current = Settings.model_validate(self._overrides)

    async def load(self) -> Settings:
        def read(conn: Any) -> dict[str, Any]:
            return {
                r["key"]: json.loads(r["value_json"]) for r in conn.execute("SELECT * FROM setting")
            }

        stored = await self._db.read(read)
        known = {k: v for k, v in stored.items() if k in Settings.model_fields}
        try:
            self.current = Settings.model_validate(deep_merge(self._overrides, known))
        except ValidationError:
            # A hand-edited or downgraded setting must not stop the engine from starting.
            self.current = Settings.model_validate(self._overrides)
        return self.current

    async def update(self, patch: dict[str, Any]) -> Settings:
        """Validate ``patch`` merged onto the current settings, then persist what changed."""
        merged = deep_merge(self.current.model_dump(mode="json"), patch)
        new = Settings.model_validate(merged)  # raises ValidationError on bad input
        changed = {key: getattr(new, key) for key in patch if key in Settings.model_fields}
        dumped = new.model_dump(mode="json")

        def write(conn: Any) -> None:
            for key in changed:
                conn.execute(
                    "INSERT INTO setting(key, value_json) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                    (key, json.dumps(dumped[key])),
                )

        await self._db.write(write)
        self.current = new
        return new

    # -- engine state -------------------------------------------------------------------
    # Small facts the engine must remember across restarts (when the last backup ran, ...).
    # They live beside the settings but are not settings: ``load`` only reads keys that are
    # fields of ``Settings`` and ``update`` only writes keys it was given, so these never show
    # up in ``GET /settings`` and cannot be set through ``PUT /settings``.

    async def get_state(self, name: str) -> str | None:
        def read(conn: Any) -> str | None:
            row = conn.execute(
                "SELECT value_json FROM setting WHERE key=?", (STATE_PREFIX + name,)
            ).fetchone()
            return None if row is None else str(json.loads(row["value_json"]))

        return await self._db.read(read)

    async def set_state(self, name: str, value: str) -> None:
        await self._db.write(
            lambda conn: conn.execute(
                "INSERT INTO setting(key, value_json) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                (STATE_PREFIX + name, json.dumps(value)),
            )
        )
