from __future__ import annotations

import os
import time
from pathlib import PurePosixPath
from zoneinfo import ZoneInfo

import pytest

from pocket_sync.models import RecordingSummary
from pocket_sync.paths import Storage, StorageError, audio_extension, relative_dir, slugify
from pocket_sync.state import MIGRATIONS, StateDB
from pocket_sync.writer import PartFile, SizeMismatch, cleanup_stale_parts, write_bytes_atomic

# --- paths ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "slug"),
    [
        ("Zażółć gęślą jaźń", "zazolc-gesla-jazn"),
        ("ŁÓDŹ — Śniadanie & Żółw!", "lodz-sniadanie-zolw"),
        ("  Plany   przyjazdu  ", "plany-przyjazdu"),
        ("", "untitled"),
        (None, "untitled"),
        ("🎤 ???", "untitled"),
    ],
)
def test_slugify(title, slug):
    assert slugify(title) == slug


def test_slugify_truncates_to_60_without_trailing_dash():
    slug = slugify("słowo " * 30)
    assert len(slug) <= 60
    assert not slug.endswith("-")
    assert slug.startswith("slowo-slowo")


def test_relative_dir_uses_local_time_and_id():
    rec = RecordingSummary(id="desktop_1_abc", title="Plany przyjazdu", recording_at="2026-09-25T08:09:00Z")
    rel = relative_dir(rec, ZoneInfo("Europe/Warsaw"))
    assert rel == PurePosixPath("2026/09/2026-09-25_1009_plany-przyjazdu_desktop_1_abc")


def test_relative_dir_sanitizes_id():
    rec = RecordingSummary(id="a/b c", title=None, created_at="2026-01-01T00:00:00Z")
    assert relative_dir(rec).name == "2026-01-01_0000_untitled_a-b-c"


def test_same_relative_path_in_both_trees(tmp_path):
    storage = Storage(tmp_path / "ssd" / "meta", tmp_path / "hdd" / "audio")
    rel = PurePosixPath("2026/09/x_id")
    assert storage.meta_dir(rel).relative_to(tmp_path / "ssd" / "meta") == storage.audio_dir(rel).relative_to(
        tmp_path / "hdd" / "audio"
    )


@pytest.mark.parametrize(
    ("url", "ctype", "ext"),
    [
        ("https://s3/u/2026/desktop_1.ogg?X-Amz=1", None, ".ogg"),
        ("https://s3/u/2026/uuid.mp3?sig", None, ".mp3"),
        ("https://s3/u/2026/noext?sig", "audio/mpeg", ".mp3"),
        ("https://s3/u/2026/noext", None, ".bin"),
    ],
)
def test_audio_extension(url, ctype, ext):
    assert audio_extension(url, ctype) == ext


def test_ensure_writable_raises_storage_error(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(StorageError):
        Storage.ensure_writable(blocker / "sub", "AUDIO_DIR")


# --- writer --------------------------------------------------------------------------------


def test_interrupted_download_leaves_no_final_file(tmp_path):
    final = tmp_path / "audio.ogg"
    part = PartFile(final)
    part.open()
    part.write(b"half")
    # process dies here: no commit
    assert not final.exists()
    assert part.part_path.exists()
    part.abort()
    assert not part.part_path.exists()


def test_size_mismatch_removes_part(tmp_path):
    final = tmp_path / "audio.ogg"
    part = PartFile(final)
    part.open()
    part.write(b"123")
    with pytest.raises(SizeMismatch):
        part.commit(expected_size=10)
    assert not final.exists()
    assert not part.part_path.exists()


def test_commit_renames_and_hashes(tmp_path):
    final = tmp_path / "audio.ogg"
    part = PartFile(final)
    part.open()
    part.write(b"abc")
    part.commit(expected_size=3)
    assert final.read_bytes() == b"abc"
    assert part.sha256 == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_write_bytes_atomic_skips_identical_content(tmp_path):
    p = tmp_path / "a" / "x.json"
    assert write_bytes_atomic(p, b"1") is True
    assert write_bytes_atomic(p, b"1") is False
    assert write_bytes_atomic(p, b"2") is True
    assert p.read_bytes() == b"2"
    assert not list(tmp_path.rglob("*.tmp"))


def test_cleanup_stale_parts(tmp_path):
    old = tmp_path / "2026/09/a/audio.ogg.part"
    new = tmp_path / "2026/09/b/audio.ogg.part"
    for p in (old, new):
        p.parent.mkdir(parents=True)
        p.write_bytes(b"x")
    past = time.time() - 25 * 3600
    os.utime(old, (past, past))
    assert cleanup_stale_parts(tmp_path) == [old]
    assert new.exists()


# --- state ---------------------------------------------------------------------------------


def test_migrate_empty_db_and_rerun_is_harmless(tmp_path):
    path = tmp_path / "state" / "pocket-sync.db"
    with StateDB(path) as db:
        assert db.schema_version() == 0
        db.migrate()
        assert db.schema_version() == len(MIGRATIONS)
        db.upsert_recording("r1", rel_dir="2026/09/x", title="t")
    with StateDB(path) as db:
        db.migrate()
        assert db.schema_version() == len(MIGRATIONS)
        assert db.get_recording("r1")["title"] == "t"
        assert db.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_upsert_rejects_unknown_columns(db):
    with pytest.raises(ValueError):
        db.upsert_recording("r1", rel_dir="x", nope=1)
