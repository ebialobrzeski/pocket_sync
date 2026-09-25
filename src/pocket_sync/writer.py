"""Atomic file writes: temp file in the target directory, fsync, os.replace, fsync directory."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, BinaryIO

PART_SUFFIX = ".part"


class SizeMismatch(Exception):
    """Downloaded byte count differs from Content-Length."""


def fsync_dir(path: Path) -> None:
    # Directory fsync is a POSIX concept; Windows cannot open directories this way.
    if os.name != "posix":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_bytes_atomic(path: Path, data: bytes, *, skip_if_same: bool = True) -> bool:
    """Write `data` to `path` atomically. Returns False when the file already had this content."""
    if skip_if_same and path.is_file():
        try:
            if path.stat().st_size == len(data) and path.read_bytes() == data:
                return False
        except OSError:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fsync_dir(path.parent)
    return True


def write_text_atomic(path: Path, text: str) -> bool:
    return write_bytes_atomic(path, text.encode("utf-8"))


def dumps_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str) + "\n"


def write_json_atomic(path: Path, obj: Any) -> bool:
    return write_text_atomic(path, dumps_json(obj))


def canonical_hash(obj: Any) -> str:
    """SHA-256 of canonical JSON (sorted keys, no whitespace)."""
    data = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


class PartFile:
    """Streams into `<final>.part`, hashing on the fly; `commit()` renames it to `final`.

    The final file never exists in a partial state: either the rename happened after a full,
    size-checked, fsynced write, or it did not happen at all.
    """

    def __init__(self, final_path: Path) -> None:
        self.final_path = final_path
        self.part_path = final_path.with_name(final_path.name + PART_SUFFIX)
        self._fh: BinaryIO | None = None
        self._sha = hashlib.sha256()
        self.size = 0
        self.sha256: str | None = None

    def open(self) -> None:
        self.part_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.part_path, "wb")  # truncates leftovers of an interrupted attempt
        self._sha = hashlib.sha256()
        self.size = 0

    def write(self, chunk: bytes) -> None:
        assert self._fh is not None, "PartFile.open() not called"
        self._fh.write(chunk)
        self._sha.update(chunk)
        self.size += len(chunk)

    def commit(self, expected_size: int | None = None) -> None:
        assert self._fh is not None, "PartFile.open() not called"
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()
        self._fh = None
        if expected_size is not None and self.size != expected_size:
            self.abort()
            raise SizeMismatch(f"expected {expected_size} bytes, got {self.size}")
        os.replace(self.part_path, self.final_path)
        fsync_dir(self.final_path.parent)
        self.sha256 = self._sha.hexdigest()

    def abort(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None
        self.part_path.unlink(missing_ok=True)


def find_part_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob(f"*{PART_SUFFIX}") if p.is_file())


def cleanup_stale_parts(root: Path, max_age_hours: float = 24) -> list[Path]:
    """Delete `.part` files older than `max_age_hours`. Returns the removed paths."""
    cutoff = time.time() - max_age_hours * 3600
    removed: list[Path] = []
    for p in find_part_files(root):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed.append(p)
        except FileNotFoundError:
            continue
    return removed
