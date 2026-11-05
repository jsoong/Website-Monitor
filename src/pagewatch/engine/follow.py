"""Follow-Links (spec: Multi-page sources): "discover links matching include and exclude patterns,
to depth 1 by default and at most 50 pages. New matches become child bookmarks in a generated
folder that inherits the parent's settings."

How it works
------------
* After a check of a bookmark with ``fetch.follow`` stored a new version, the page's links are
  extracted in a worker and filtered: same host, include / exclude patterns, not seen before, not
  already a bookmark, allowed by robots.txt. Up to ``max_pages`` of them become bookmarks.
* The children live in one generated folder next to the parent. The folder's defaults are the
  parent's own stored settings (without ``follow`` and ``merge``), re-synced on every discovery
  and whenever the parent is edited, so a change to the parent's keywords reaches its children.
* ``depth`` > 1: each child remembers (kv ``follow_lineage``) its root and how many levels are
  left, and discovers links on its own page in turn. The page limit is one budget for the whole
  crawl, kept on the root (kv ``follow_state``). A URL is created at most once: a child the owner
  deleted does not come back.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from functools import partial
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from pagewatch.engine import bookmarks as svc
from pagewatch.engine import kv
from pagewatch.engine.clock import parse_iso
from pagewatch.engine.config import resolve
from pagewatch.engine.logs import get_logger
from pagewatch.engine.pipeline.links import extract_links
from pagewatch.engine.robots import RobotsCache
from pagewatch.engine.store import repo
from pagewatch.engine.urls import host_key, normalize_url
from pagewatch.models import (
    BookmarkIn,
    FollowLinksConfig,
    Trigger,
    dumps,
    loads,
)

if TYPE_CHECKING:
    from pagewatch.engine.core import Engine

log = get_logger("pagewatch.follow")

SEEN_CAP = 5000
# Settings of the parent that its children do not inherit: they would follow, merge and parse
# records exactly like the page that lists them.
_NOT_INHERITED = ("follow", "merge", "records")
_FOLDER_SCALARS = ("check_method", "highlight_mode", "priority")
_SECTIONS = ("schedule", "fetch", "filter", "gate", "actions")


def compile_patterns(patterns: list[str], kind: str) -> list[re.Pattern[str]]:
    """Wildcard (``*``, ``?``) or regex patterns, matched anywhere in the URL (any case)."""
    out: list[re.Pattern[str]] = []
    for p in patterns:
        if kind == "regex":
            out.append(re.compile(p, re.IGNORECASE))
        else:
            body = re.escape(p).replace(r"\*", ".*").replace(r"\?", ".")
            out.append(re.compile(body, re.IGNORECASE))
    return out


def select_links(
    links: list[tuple[str, str]],
    cfg: FollowLinksConfig,
    page_url: str,
    *,
    seen: set[str],
    existing: set[str],
) -> list[tuple[str, str]]:
    """The discovered links that pass the include / exclude patterns, the same-host rule and the
    memory of what already exists (everything is compared by normalised URL)."""
    include = compile_patterns(cfg.include, cfg.pattern_kind)
    exclude = compile_patterns(cfg.exclude, cfg.pattern_kind)
    here = host_key(page_url)
    me = normalize_url(page_url)
    out: list[tuple[str, str]] = []
    taken: set[str] = set()
    for url, text in links:
        key = normalize_url(url)
        if key == me or key in seen or key in existing or key in taken:
            continue
        if cfg.same_host and host_key(url) != here:
            continue
        if include and not any(p.search(url) for p in include):
            continue
        if any(p.search(url) for p in exclude):
            continue
        taken.add(key)
        out.append((url, text))
    return out


def child_defaults(root: sqlite3.Row) -> dict[str, Any]:
    """What the generated folder passes to the children: the parent's stored overrides."""
    defaults: dict[str, Any] = {}
    for section in _SECTIONS:
        value = loads(root[f"{section}_json"], {})
        if section == "fetch" and isinstance(value, dict):
            value = {k: v for k, v in value.items() if k not in _NOT_INHERITED}
        if section == "actions" and isinstance(value, list):
            value = {"actions": value}
        if value:
            defaults[section] = value
    for scalar in _FOLDER_SCALARS:
        defaults[scalar] = root[scalar]
    return defaults


class FollowService:
    def __init__(self, engine: Engine) -> None:
        self.e = engine
        self.robots = RobotsCache(engine)
        self._locks: dict[int, asyncio.Lock] = {}

    # -- entry points -----------------------------------------------------------------------

    def after_check(self, bookmark_id: int, body: bytes, content_type: str, final_url: str) -> None:
        """The runner stored a new version of this bookmark: look for links in the background."""
        self.e.spawn(
            self.discover(bookmark_id, body, content_type, final_url), f"follow-{bookmark_id}"
        )

    async def rediscover(self, bookmark_id: int) -> int:
        """Run discovery again over the stored page (the follow settings were edited)."""
        row = await self.e.db.read(lambda c: repo.bookmark_get(c, bookmark_id))
        if row is None or not row["latest_version_id"]:
            return 0
        version = await self.e.db.read(lambda c: repo.version_get(c, row["latest_version_id"]))
        if version is None or not version["raw_hash"]:
            return 0
        body = await asyncio.to_thread(self.e.blobs.get, version["raw_hash"])
        return await self.discover(bookmark_id, body, version["content_type"] or "", row["url"])

    async def sync_folder(self, bookmark_id: int) -> None:
        """The parent was edited: refresh what its children inherit."""
        state = await self.e.db.read(lambda c: kv.get(c, kv.FOLLOW_STATE, bookmark_id))
        row = await self.e.db.read(lambda c: repo.bookmark_get(c, bookmark_id))
        if not state or row is None or not state.get("folder"):
            return
        await self._sync(row, int(state["folder"]))

    # -- discovery --------------------------------------------------------------------------

    async def discover(
        self, bookmark_id: int, body: bytes, content_type: str, final_url: str
    ) -> int:
        # The page limit is one budget for the whole crawl, so discoveries of a root and of all
        # its descendants take turns: the lock belongs to the root, not to the page.
        lineage = await self.e.db.read(lambda c: kv.get(c, kv.FOLLOW_LINEAGE, bookmark_id))
        root_id = int(lineage["root"]) if lineage else bookmark_id
        async with self._locks.setdefault(root_id, asyncio.Lock()):
            try:
                return await self._discover(bookmark_id, lineage, body, content_type, final_url)
            except (ValidationError, svc.InvalidError, svc.NotFoundError, OSError) as exc:
                log.warning("follow_failed", bookmark_id=bookmark_id, error=str(exc)[:200])
                return 0

    async def _discover(
        self,
        bookmark_id: int,
        lineage: dict[str, Any] | None,
        body: bytes,
        content_type: str,
        final_url: str,
    ) -> int:
        e = self.e
        row = await e.db.read(lambda c: repo.bookmark_get(c, bookmark_id))
        if row is None:
            return 0
        if lineage:  # a child: the crawl belongs to its root
            root = await e.db.read(lambda c: repo.bookmark_get(c, int(lineage["root"])))
            depth_left = int(lineage["depth_left"])
        else:
            root, depth_left = row, 0
        if root is None:
            return 0
        cfg = resolve(root, e.folders, e.settings).fetch.follow
        if cfg is None or not cfg.enabled:
            return 0
        if not lineage:
            depth_left = cfg.depth
        if depth_left < 1:
            return 0
        root_id = int(root["id"])
        state = await e.db.read(lambda c: kv.get(c, kv.FOLLOW_STATE, root_id, {}))
        state = {"folder": None, "seen": [], "created": 0, **(state or {})}
        budget = cfg.max_pages - int(state["created"])
        if budget <= 0:
            return 0

        links = await e.pool.run(extract_links, body, content_type, final_url)
        existing = await e.db.read(
            lambda c: {normalize_url(r["url"]) for r in c.execute("SELECT url FROM bookmark")}
        )
        candidates = select_links(links, cfg, final_url, seen=set(state["seen"]), existing=existing)
        chosen: list[tuple[str, str]] = []
        for url, text in candidates:  # robots.txt is asked about the survivors only
            if len(chosen) >= budget:
                break
            if await self.robots.allowed(url):
                chosen.append((url, text))
        if not chosen:
            return 0

        folder_id = await self._folder(root, cfg, state)
        created: list[int] = []
        for url, text in chosen:
            child = await self._create_child(root, folder_id, url, text)
            if child is None:
                continue
            created.append(child)
            lineage_value = {"root": root_id, "depth_left": depth_left - 1}
            await e.db.write(partial(kv.put, ns=kv.FOLLOW_LINEAGE, key=child, value=lineage_value))
        seen = [*state["seen"], *(normalize_url(u) for u, _ in chosen)][-SEEN_CAP:]
        state.update(folder=folder_id, seen=seen, created=int(state["created"]) + len(created))
        await e.db.write(lambda c: kv.put(c, kv.FOLLOW_STATE, root_id, state))
        log.info("follow_created", parent=bookmark_id, root=root_id, children=len(created))
        return len(created)

    # -- the generated folder and the children ----------------------------------------------

    async def _folder(
        self, root: sqlite3.Row, cfg: FollowLinksConfig, state: dict[str, Any]
    ) -> int:
        e = self.e
        existing = state.get("folder")
        if existing is not None and e.folders.exists(int(existing)):
            await self._sync(root, int(existing))
            return int(existing)
        data = {
            "parent_id": root["folder_id"],
            "name": cfg.folder_name or f"{root['name']} (followed)",
            "defaults_json": dumps(child_defaults(root)),
        }
        folder_id = await e.db.write(lambda c: repo.folder_insert(c, data))
        await e.reload_folders()
        return folder_id

    async def _sync(self, root: sqlite3.Row, folder_id: int) -> None:
        e = self.e
        wanted = dumps(child_defaults(root))
        current = await e.db.read(lambda c: repo.folder_get(c, folder_id))
        if current is None or current["defaults_json"] == wanted:
            return
        await e.db.write(lambda c: repo.folder_update(c, folder_id, {"defaults_json": wanted}))
        await e.reload_folders()

    async def _create_child(
        self, root: sqlite3.Row, folder_id: int, url: str, text: str
    ) -> int | None:
        e = self.e
        try:
            body = BookmarkIn(
                url=url,
                name=text[:300] or None,
                folder_id=folder_id,
                plugin=root["plugin"],
                macro_id=root["macro_id"],
            )
            row = await svc.create(e, body)
        except (ValidationError, svc.InvalidError, svc.NotFoundError) as exc:
            log.warning("follow_child_rejected", url=url, error=str(exc)[:160])
            return None
        due = parse_iso(row["next_due_at"]) if row["next_due_at"] else None
        e.scheduler.upsert(
            row["id"],
            url=row["url"],
            priority=row["priority"],
            check_method=row["check_method"],
            enabled=bool(row["enabled"]),
            due=due,
            trigger=Trigger.FOLLOW,
        )
        return int(row["id"])
