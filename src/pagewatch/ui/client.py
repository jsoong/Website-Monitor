"""Typed, synchronous client for the engine's local API.

The UI never fetches pages itself. Calls block, so the UI runs them on worker threads
(``ui.workers.run_async``); ``httpx.Client`` is thread-safe. Every response is validated
into the shared models of ``pagewatch.models``.
"""

from __future__ import annotations

from typing import Any

import httpx

from pagewatch.cli.client import ApiError, EngineNotRunning
from pagewatch.engine.instance import read_lockfile
from pagewatch.engine.paths import DataDir
from pagewatch.models import (
    AutowatchState,
    BookmarkCounts,
    BookmarkOut,
    BookmarkSummary,
    ChangeOut,
    CheckRunOut,
    FalsePositiveOut,
    FolderOut,
    HealthOut,
    Page,
    PreviewOut,
    RenderOut,
    Settings,
    TestFilterOut,
)

__all__ = ["ApiClient", "ApiError", "EngineNotRunning", "connect"]


class ApiClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 30.0) -> None:
        self.base_url = base_url
        self.token = token
        self._http = httpx.Client(
            base_url=base_url, headers={"Authorization": f"Bearer {token}"}, timeout=timeout
        )

    @property
    def ws_url(self) -> str:
        return self.base_url.replace("http://", "ws://", 1) + "/events"

    def close(self) -> None:
        self._http.close()

    def _call(self, method: str, path: str, **kw: Any) -> Any:
        try:
            resp = self._http.request(method, path, **kw)
        except httpx.TransportError as exc:
            raise EngineNotRunning(f"cannot reach the engine: {exc}") from exc
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text
            raise ApiError(resp.status_code, str(detail))
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    @staticmethod
    def _clean(params: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in params.items() if v is not None and v != ""}

    # -- engine -------------------------------------------------------------------------

    def health(self) -> HealthOut:
        return HealthOut.model_validate(self._call("GET", "/health"))

    def autowatch(self, state: str, until: str | None = None) -> AutowatchState:
        body = {"state": state, "until": until}
        return AutowatchState.model_validate(self._call("POST", "/autowatch", json=body))

    def settings(self) -> Settings:
        return Settings.model_validate(self._call("GET", "/settings"))

    # -- folders ------------------------------------------------------------------------

    def folders(self) -> list[FolderOut]:
        return [FolderOut.model_validate(f) for f in self._call("GET", "/folders")]

    # -- bookmarks ----------------------------------------------------------------------

    def bookmarks(self, **params: Any) -> Page[BookmarkSummary]:
        return Page[BookmarkSummary].model_validate(
            self._call("GET", "/bookmarks", params=self._clean(params))
        )

    def counts(self) -> BookmarkCounts:
        return BookmarkCounts.model_validate(self._call("GET", "/bookmarks/counts"))

    def bookmark(self, bookmark_id: int) -> BookmarkOut:
        return BookmarkOut.model_validate(self._call("GET", f"/bookmarks/{bookmark_id}"))

    def create_bookmark(self, body: dict[str, Any]) -> BookmarkOut:
        return BookmarkOut.model_validate(self._call("POST", "/bookmarks", json=body))

    def patch_bookmark(self, bookmark_id: int, patch: dict[str, Any]) -> BookmarkOut:
        return BookmarkOut.model_validate(
            self._call("PATCH", f"/bookmarks/{bookmark_id}", json=patch)
        )

    def bulk(self, ids: list[int], action: str, **extra: Any) -> int:
        out = self._call("POST", "/bookmarks/bulk", json={"ids": ids, "action": action, **extra})
        return int(out["affected"])

    def delete_bookmark(self, bookmark_id: int) -> None:
        self._call("DELETE", f"/bookmarks/{bookmark_id}")

    def mark_read(self, bookmark_id: int) -> BookmarkOut:
        return BookmarkOut.model_validate(self._call("POST", f"/bookmarks/{bookmark_id}/read"))

    def check(self, ids: list[int] | None = None, *, all: bool = False, force: bool = False) -> int:
        body: dict[str, Any] = {"force": force}
        if all:
            body["all"] = True
        else:
            body["ids"] = ids or []
        return int(self._call("POST", "/check", json=body)["queued"])

    # -- changes and views --------------------------------------------------------------

    def changes(self, bookmark_id: int, limit: int = 50) -> Page[ChangeOut]:
        return Page[ChangeOut].model_validate(
            self._call("GET", f"/bookmarks/{bookmark_id}/changes", params={"limit": limit})
        )

    def runs(self, bookmark_id: int, limit: int = 100) -> Page[CheckRunOut]:
        return Page[CheckRunOut].model_validate(
            self._call("GET", f"/bookmarks/{bookmark_id}/runs", params={"limit": limit})
        )

    def unread_diff(
        self,
        bookmark_id: int,
        view: str = "highlight",
        *,
        images: bool = False,
        context: int | None = None,
    ) -> RenderOut:
        params = self._clean(
            {"view": view, "format": "json", "images": images or None, "context": context}
        )
        return RenderOut.model_validate(
            self._call("GET", f"/bookmarks/{bookmark_id}/diff", params=params)
        )

    def change_render(
        self,
        change_id: int,
        view: str = "highlight",
        *,
        images: bool = False,
        context: int | None = None,
    ) -> RenderOut:
        params = self._clean(
            {"view": view, "format": "json", "images": images or None, "context": context}
        )
        return RenderOut.model_validate(
            self._call("GET", f"/changes/{change_id}/render", params=params)
        )

    def false_positive(self, change_id: int) -> FalsePositiveOut:
        return FalsePositiveOut.model_validate(
            self._call("POST", f"/changes/{change_id}/false-positive")
        )

    def test_filter(self, bookmark_id: int, candidate: dict[str, Any]) -> TestFilterOut:
        return TestFilterOut.model_validate(
            self._call("POST", f"/bookmarks/{bookmark_id}/test-filter", json=candidate)
        )

    def preview(self, url: str, *, samples: int = 2, gap_s: float = 5.0) -> PreviewOut:
        body = {"url": url, "samples": samples, "gap_s": gap_s}
        return PreviewOut.model_validate(self._call("POST", "/preview", json=body, timeout=60))


def connect(data_dir: DataDir) -> ApiClient:
    """Discover the running engine through its lockfile."""
    info = read_lockfile(data_dir)
    if info is None:
        raise EngineNotRunning(f"no PageWatch engine is running for {data_dir.root}")
    return ApiClient(f"http://127.0.0.1:{info.port}", info.token)
