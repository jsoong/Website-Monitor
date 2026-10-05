"""Content-addressed blob store: ``blobs/ab/cd/<sha256>.zst``.

A blob is named by the SHA-256 of its *uncompressed* content, so equal content is stored
once and "is this the same as before?" is answered by comparing names without reading a
file. Writes are atomic (temp file, fsync, rename) and happen before any database row
references the blob; a crash can therefore leave an orphan but never a dangling reference.

The store is safe to use from several processes: worker processes read and write blobs
directly so only small results cross process boundaries.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import zstandard

_MAX_DECOMPRESSED = 512 * 1024 * 1024
_HEX = frozenset("0123456789abcdef")


class BlobNotFound(KeyError):
    pass


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _valid(digest: str) -> bool:
    return len(digest) == 64 and _HEX.issuperset(digest)


class BlobStore:
    def __init__(self, root: Path, *, level: int = 3) -> None:
        self.root = root
        self.level = level

    def path_for(self, digest: str) -> Path:
        if not _valid(digest):
            raise ValueError(f"not a sha256 digest: {digest!r}")
        return self.root / digest[:2] / digest[2:4] / f"{digest}.zst"

    # -- write --------------------------------------------------------------------------

    def put(self, data: bytes) -> str:
        digest = sha256_hex(data)
        self._write(digest, data)
        return digest

    def put_text(self, text: str) -> str:
        return self.put(text.encode("utf-8"))

    def put_json(self, obj: Any) -> str:
        return self.put(json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))

    def put_with_digest(self, digest: str, data: bytes) -> None:
        """Store ``data`` under a digest the caller already computed (avoids re-hashing)."""
        if not _valid(digest):
            raise ValueError(f"not a sha256 digest: {digest!r}")
        self._write(digest, data)

    def _write(self, digest: str, data: bytes) -> None:
        path = self.path_for(digest)
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        packed = zstandard.ZstdCompressor(level=self.level).compress(data)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            with open(tmp, "wb") as fh:
                fh.write(packed)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise
        self._fsync_dir(path.parent)

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        if os.name == "nt":  # directories cannot be opened for fsync on Windows
            return
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    # -- read ---------------------------------------------------------------------------

    def get(self, digest: str, *, verify: bool = False) -> bytes:
        path = self.path_for(digest)
        try:
            packed = path.read_bytes()
        except FileNotFoundError:
            raise BlobNotFound(digest) from None
        data = zstandard.ZstdDecompressor().decompress(packed, max_output_size=_MAX_DECOMPRESSED)
        if verify and sha256_hex(data) != digest:
            raise ValueError(f"blob {digest} is corrupt")
        return data

    def get_text(self, digest: str) -> str:
        return self.get(digest).decode("utf-8")

    def get_json(self, digest: str) -> Any:
        return json.loads(self.get(digest))

    def exists(self, digest: str) -> bool:
        return self.path_for(digest).exists()

    # -- maintenance --------------------------------------------------------------------

    def delete(self, digest: str) -> int:
        """Remove a blob; returns the bytes freed (0 if it was already gone)."""
        path = self.path_for(digest)
        try:
            size = path.stat().st_size
            path.unlink()
        except FileNotFoundError:
            return 0
        return size

    def iter_blobs(self) -> Iterator[tuple[str, int]]:
        """Yield ``(digest, compressed_size)`` for every stored blob."""
        if not self.root.exists():
            return
        for path in self.root.glob("??/??/*.zst"):
            digest = path.stem
            if _valid(digest):
                try:
                    yield digest, path.stat().st_size
                except FileNotFoundError:
                    continue

    def iter_stale_temp_files(self, older_than_s: float) -> Iterator[Path]:
        import time

        cutoff = time.time() - older_than_s
        for path in self.root.glob("??/??/*.tmp"):
            try:
                if path.stat().st_mtime < cutoff:
                    yield path
            except FileNotFoundError:
                continue

    def total_size(self) -> int:
        return sum(size for _, size in self.iter_blobs())
