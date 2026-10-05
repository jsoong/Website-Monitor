import hashlib
from pathlib import Path

import pytest

from pagewatch.engine.store.blobs import BlobNotFound, BlobStore, sha256_hex


def test_roundtrip_and_naming(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    data = b"hello " * 1000
    digest = store.put(data)
    assert digest == hashlib.sha256(data).hexdigest() == sha256_hex(data)
    path = store.path_for(digest)
    assert path == tmp_path / digest[:2] / digest[2:4] / f"{digest}.zst"
    assert path.stat().st_size < len(data)  # compressed
    assert store.get(digest) == data
    assert store.get(digest, verify=True) == data


def test_identical_content_is_stored_once(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    a, b = store.put(b"same"), store.put(b"same")
    assert a == b
    assert len(list(store.iter_blobs())) == 1


def test_text_and_json_helpers(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    assert store.get_text(store.put_text("héllo")) == "héllo"
    assert store.get_json(store.put_json({"a": [1, 2]})) == {"a": [1, 2]}


def test_missing_and_invalid_digests(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    with pytest.raises(BlobNotFound):
        store.get("0" * 64)
    with pytest.raises(ValueError):
        store.path_for("../../etc/passwd")
    with pytest.raises(ValueError):
        store.put_with_digest("nope", b"x")


def test_corruption_detected_when_verifying(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    digest = store.put(b"original")
    other = store.put(b"tampered")
    store.path_for(digest).write_bytes(store.path_for(other).read_bytes())
    with pytest.raises(ValueError):
        store.get(digest, verify=True)


def test_no_temp_files_left_behind_and_delete(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    digest = store.put(b"x" * 100)
    assert not list(tmp_path.rglob("*.tmp"))
    assert store.delete(digest) > 0
    assert store.delete(digest) == 0
    assert not store.exists(digest)


def test_failed_write_leaves_no_blob_or_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BlobStore(tmp_path)

    def boom(_fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("os.fsync", boom)
    with pytest.raises(OSError):
        store.put(b"data")
    assert not list(tmp_path.rglob("*.zst"))
    assert not list(tmp_path.rglob("*.tmp"))
