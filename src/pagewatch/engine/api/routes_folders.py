from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import ValidationError

from pagewatch.engine.api.app_deps import engine_dep
from pagewatch.engine.api.serializers import folder_out
from pagewatch.engine.config import validate_sections
from pagewatch.engine.core import Engine
from pagewatch.engine.store import repo
from pagewatch.models import FolderIn, FolderOut, FolderPatch, apply_patch, dumps, loads

router = APIRouter(prefix="/folders", tags=["folders"])
EngineDep = Annotated[Engine, Depends(engine_dep)]


def _validate_defaults(engine: Engine, defaults: dict[str, Any], parent_id: int | None) -> None:
    unknown = set(defaults) - {
        "schedule", "fetch", "filter", "gate", "actions",
        "check_method", "source_type", "highlight_mode", "priority",
    }  # fmt: skip
    if unknown:
        raise HTTPException(422, f"unknown folder default(s): {sorted(unknown)}")
    try:
        validate_sections(defaults, parent_id, engine.folders, engine.settings)
    except ValidationError as exc:
        raise HTTPException(422, str(exc.errors()[0]["msg"])) from exc


@router.get("", response_model=list[FolderOut])
async def list_folders(engine: EngineDep) -> list[FolderOut]:
    rows = await engine.db.read(repo.folder_list)
    return [folder_out(r) for r in rows]


@router.post("", response_model=FolderOut, status_code=201)
async def create_folder(body: FolderIn, engine: EngineDep) -> FolderOut:
    if body.parent_id is not None and not engine.folders.exists(body.parent_id):
        raise HTTPException(422, f"parent folder {body.parent_id} does not exist")
    _validate_defaults(engine, body.defaults, body.parent_id)
    data = {
        "parent_id": body.parent_id,
        "name": body.name,
        "sort_order": body.sort_order,
        "is_virtual": body.is_virtual,
        "query_json": dumps(body.query) if body.query is not None else None,
        "defaults_json": dumps(body.defaults),
    }
    row = await engine.db.write(lambda c: repo.folder_get(c, repo.folder_insert(c, data)))
    assert row is not None
    await engine.reload_folders()
    return folder_out(row)


@router.patch("/{folder_id}", response_model=FolderOut)
async def patch_folder(folder_id: int, body: FolderPatch, engine: EngineDep) -> FolderOut:
    row = await engine.db.read(lambda c: repo.folder_get(c, folder_id))
    if row is None:
        raise HTTPException(404, "folder not found")
    fields: dict[str, Any] = {}
    explicit = body.model_fields_set
    if "name" in explicit and body.name is not None:
        fields["name"] = body.name
    if "sort_order" in explicit and body.sort_order is not None:
        fields["sort_order"] = body.sort_order
    parent = row["parent_id"]
    if "parent_id" in explicit:
        if body.parent_id is not None:
            if not engine.folders.exists(body.parent_id):
                raise HTTPException(422, f"parent folder {body.parent_id} does not exist")
            if engine.folders.is_ancestor(folder_id, body.parent_id):
                raise HTTPException(422, "a folder cannot be moved into itself or its descendants")
        parent = body.parent_id
        fields["parent_id"] = parent
    if "query" in explicit:
        fields["query_json"] = dumps(body.query) if body.query is not None else None
    defaults_changed = False
    if "defaults" in explicit and body.defaults is not None:
        merged = apply_patch(loads(row["defaults_json"], {}) or {}, body.defaults)
        _validate_defaults(engine, merged, parent)
        fields["defaults_json"] = dumps(merged)
        defaults_changed = True

    def write(conn: Any) -> Any:
        repo.folder_update(conn, folder_id, fields)
        return repo.folder_get(conn, folder_id)

    updated = await engine.db.write(write)
    await engine.reload_folders()
    if defaults_changed and "filter" in (body.defaults or {}):
        engine.spawn(_rebuild_folder(engine, folder_id), f"rebuild-folder-{folder_id}")
    return folder_out(updated)


async def _rebuild_folder(engine: Engine, folder_id: int) -> None:
    """Filter defaults changed: re-normalise every bookmark that inherits them."""
    ids = await engine.db.read(
        lambda c: repo.bookmark_ids_in(c, repo.folder_with_descendants(c, folder_id))
    )
    for bid in ids:
        await engine.rebuild_versions(bid)


@router.delete("/{folder_id}", status_code=204)
async def delete_folder(folder_id: int, engine: EngineDep) -> Response:
    row = await engine.db.read(lambda c: repo.folder_get(c, folder_id))
    if row is None:
        raise HTTPException(404, "folder not found")
    # Subfolders are deleted with it; bookmarks inside fall back to the root (folder_id NULL).
    await engine.db.write(lambda c: repo.folder_delete(c, folder_id))
    await engine.reload_folders()
    return Response(status_code=204)
