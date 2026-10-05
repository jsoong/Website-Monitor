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
