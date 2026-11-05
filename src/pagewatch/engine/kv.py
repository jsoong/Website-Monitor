"""Small namespaced key-value records for features that need to remember something between
checks (plugin state, Follow-Links memory, the "last report" marker).

They live in the existing ``setting`` table under ``kv.<namespace>.<key>``. ``SettingsStore``
only reads keys that are fields of ``Settings``, so these rows never leak into the global settings,
and no schema migration is needed. Every function takes an open connection: call them inside
``Database.read`` / ``Database.write`` like everything in ``store/``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from typing import Any

PLUGIN_STATE = "plugin_state"
FOLLOW_STATE = "follow_state"
FOLLOW_LINEAGE = "follow_lineage"
REPORT_MARKER = "report_marker"

#: namespaces keyed by bookmark id; their rows go away with the bookmark
PER_BOOKMARK = (PLUGIN_STATE, FOLLOW_STATE, FOLLOW_LINEAGE)


def _key(ns: str, key: object) -> str:
    return f"kv.{ns}.{key}"


def get(conn: sqlite3.Connection, ns: str, key: object, default: Any = None) -> Any:
    row = conn.execute("SELECT value_json FROM setting WHERE key=?", (_key(ns, key),)).fetchone()
    return default if row is None else json.loads(row["value_json"])


def put(conn: sqlite3.Connection, ns: str, key: object, value: Any) -> None:
    conn.execute(
        "INSERT INTO setting(key, value_json) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
        (_key(ns, key), json.dumps(value, separators=(",", ":"), sort_keys=True)),
    )


def delete(conn: sqlite3.Connection, ns: str, key: object) -> None:
    conn.execute("DELETE FROM setting WHERE key=?", (_key(ns, key),))


def forget_bookmarks(conn: sqlite3.Connection, ids: Iterable[int]) -> None:
    """Drop every per-bookmark record of deleted bookmarks."""
    for bookmark_id in ids:
        for ns in PER_BOOKMARK:
            delete(conn, ns, bookmark_id)
