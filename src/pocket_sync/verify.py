"""Archive consistency check. Run manually or from cron: `python -m pocket_sync verify`."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from .config import Settings
from .paths import Storage
from .state import StateDB
from .writer import find_part_files, sha256_file


@dataclass(frozen=True)
class Problem:
    kind: str
    detail: str
    recording_id: str | None = None
    path: str | None = None

    def __str__(self) -> str:
        parts = [f"[{self.kind}]"]
        if self.recording_id:
            parts.append(self.recording_id)
        parts.append(self.detail)
        if self.path:
            parts.append(f"({self.path})")
        return " ".join(parts)


def _leaf_dirs(root: Path) -> set[str]:
    """Relative `YYYY/MM/<name>` directories under `root`."""
    if not root.is_dir():
        return set()
    return {
        p.relative_to(root).as_posix()
        for p in root.glob("*/*/*")
        if p.is_dir() and not p.name.startswith(".")
    }


def verify(settings: Settings, *, checksums: bool = False) -> tuple[list[Problem], int]:
    """Return (problems, number of recordings in state)."""
    problems: list[Problem] = []
    if not settings.db_path.exists():
        return [Problem("no_state", "state database does not exist", path=str(settings.db_path))], 0

    storage = Storage(settings.meta_dir, settings.audio_dir)
    check_audio = settings.download_audio
    with StateDB(settings.db_path, create=False) as db:
        rows = db.all_recordings()

    meta_dirs = _leaf_dirs(settings.meta_dir)
    audio_dirs = _leaf_dirs(settings.audio_dir) if check_audio else set()
    known_dirs = {r["rel_dir"] for r in rows.values()}

    for rid, row in sorted(rows.items(), key=lambda kv: kv[1]["rel_dir"]):
        rel = row["rel_dir"]
        meta_dir = storage.meta_dir(rel)
        if row["meta_status"] in ("done", "pending"):
            for name in ("raw.json", ".meta.json"):
                if not (meta_dir / name).is_file():
                    problems.append(Problem("missing_meta", f"{name} missing", rid, str(meta_dir / name)))

        if not check_audio or row["audio_status"] != "done":
            continue
        if not row["audio_file"]:
            problems.append(Problem("missing_audio", "audio marked done but no file name in state", rid))
            continue
        audio = storage.audio_dir(rel) / row["audio_file"]
        if not audio.is_file():
            problems.append(Problem("missing_audio", "audio file does not exist", rid, str(audio)))
            continue
        size = audio.stat().st_size
        if row["audio_bytes"] is not None and size != row["audio_bytes"]:
            problems.append(
                Problem("size_mismatch", f"expected {row['audio_bytes']} bytes, found {size}", rid, str(audio))
            )
            continue
        if checksums and row["audio_sha256"] and sha256_file(audio) != row["audio_sha256"]:
            problems.append(Problem("checksum_mismatch", "SHA-256 differs from state", rid, str(audio)))

    for rel in sorted(audio_dirs - meta_dirs):
        problems.append(
            Problem("orphan_audio_dir", "audio directory has no metadata directory", path=str(storage.audio_dir(rel)))
        )
    for rel in sorted(meta_dirs - known_dirs):
        problems.append(Problem("untracked_dir", "metadata directory not in state", path=str(storage.meta_dir(rel))))

    if check_audio:
        for part in find_part_files(settings.audio_dir):
            problems.append(Problem("partial_file", "leftover partial download", path=str(part)))
    return problems, len(rows)


def run_verify(settings: Settings, *, checksums: bool = False, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    problems, total = verify(settings, checksums=checksums)
    if not problems:
        suffix = " with checksums" if checksums else ""
        print(f"OK: {total} recording(s) verified{suffix}, no problems.", file=out)
        return 0
    print(f"FOUND {len(problems)} problem(s) in {total} recording(s):", file=out)
    for p in problems:
        print(f"  {p}", file=out)
    return 1
