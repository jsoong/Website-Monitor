"""Synchronous client for the engine's local API, used by the CLI (and scripts)."""

from __future__ import annotations

from typing import Any

import httpx

from pagewatch.engine.instance import read_lockfile
from pagewatch.engine.paths import DataDir


class EngineNotRunning(RuntimeError):
    pass


class ApiError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


class EngineClient:
    def __init__(self, data_dir: DataDir, *, timeout: float = 30.0) -> None:
        info = read_lockfile(data_dir)
        if info is None:
            raise EngineNotRunning(
                f"no PageWatch engine is running for {data_dir.root}. "
                "Start it with 'pagewatch-engine'."
            )
        self.info = info
        self._http = httpx.Client(
            base_url=f"http://127.0.0.1:{info.port}",
            headers={"Authorization": f"Bearer {info.token}"},
            timeout=timeout,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> EngineClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            resp = self._http.request(method, path, **kwargs)
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

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params={k: v for k, v in params.items() if v is not None})

    def post(self, path: str, json: Any = None, **params: Any) -> Any:
        return self.request("POST", path, json=json, params=params or None)

    def patch(self, path: str, json: Any) -> Any:
        return self.request("PATCH", path, json=json)

    def delete(self, path: str) -> Any:
        return self.request("DELETE", path)

    def iter_bookmarks(self, **params: Any) -> list[dict[str, Any]]:
        """Every bookmark matching the filters, following the cursor."""
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = self.get("/bookmarks", limit=500, cursor=cursor, **params)
            out.extend(page["items"])
            cursor = page["next_cursor"]
            if not cursor:
                return out
