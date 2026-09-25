from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from pocket_sync.__main__ import healthcheck, main
from pocket_sync.api import PocketClient
from pocket_sync.state import StateDB, iso
from pocket_sync.sync import ATTENTION_THRESHOLD, Syncer
from pocket_sync.verify import run_verify, verify
from pocket_sync.writer import sha256_file

from .conftest import BASE, FakePocket, details_for, no_sleep, sha


@pytest.fixture
async def syncer(settings, db):
    async with PocketClient(BASE, "pk_test", sleep=no_sleep) as api:
        yield Syncer(settings, api, db)


REL = "2026/09/2026-09-25_1009_plany-przyjazdu_r1"


async def test_first_run_writes_both_trees(fake: FakePocket, syncer: Syncer, settings, db, dirs):
    rec = fake.add("r1")
    stats = await syncer.run_once()
    assert (stats.status, stats.processed, stats.failed) == ("ok", 1, 0)

    meta = dirs["meta"] / REL
    audio = dirs["audio"] / REL / "audio.ogg"
    assert audio.read_bytes() == rec.audio
    assert not (meta / "audio.ogg").exists()
    for name in ("raw.json", "transcript.json", "transcript.md", "summary.md", "actions.json", ".meta.json"):
        assert (meta / name).is_file(), name

    row = db.get_recording("r1")
    assert row["rel_dir"] == REL
    assert row["audio_status"] == "done" and row["meta_status"] == "done"
    assert row["audio_sha256"] == sha(rec.audio) == sha256_file(audio)
    assert row["audio_bytes"] == len(rec.audio)

    meta_json = json.loads((meta / ".meta.json").read_text(encoding="utf-8"))
    assert meta_json["audio_rel_path"] == f"{REL}/audio.ogg"
    assert meta_json["audio_sha256"] == row["audio_sha256"]
    assert "Kiedy przyjeżdżacie?" in (meta / "transcript.md").read_text(encoding="utf-8")
    assert json.loads((meta / "actions.json").read_text(encoding="utf-8"))[0]["label"] == "Zarezerwować hotel"
    assert json.loads((meta / "raw.json").read_text(encoding="utf-8")) == rec.details


async def test_second_run_is_idempotent(fake: FakePocket, syncer: Syncer, dirs):
    fake.add("r1")
    fake.add("r2", recording_at="2026-09-24T10:00:00Z")
    await syncer.run_once()
    meta_file = dirs["meta"] / REL / ".meta.json"
    before = meta_file.read_bytes()

    fake.reset_calls()
    stats = await syncer.run_once()
    assert (stats.processed, stats.skipped, stats.failed, stats.bytes_downloaded) == (0, 2, 0, 0)
    assert fake.calls == {"list": 1}  # only the listing, no details/audio
    assert meta_file.read_bytes() == before


async def test_summary_change_rewrites_summary_without_audio(fake: FakePocket, syncer: Syncer, dirs):
    rec = fake.add("r1")
    await syncer.run_once()
    rec.item["updated_at"] = "2026-09-25T09:00:00Z"
    rec.details = details_for(rec.item, summary="### Nowa teza\nZmienione podsumowanie.")

    fake.reset_calls()
    stats = await syncer.run_once()
    assert stats.processed == 1
    assert "Nowa teza" in (dirs["meta"] / REL / "summary.md").read_text(encoding="utf-8")
    assert fake.count("audio-url") == 0 and fake.count("s3") == 0


async def test_periodic_refresh_detects_summary_change_without_updated_at(
    fake: FakePocket, make_settings, db, dirs
):
    settings = make_settings(full_refresh_hours=1)
    now = [datetime(2026, 9, 25, 10, 0, tzinfo=UTC)]
    rec = fake.add("r1")
    async with PocketClient(BASE, "pk_test", sleep=no_sleep) as api:
        syncer = Syncer(settings, api, db, clock=lambda: now[0])
        await syncer.run_once()
        rec.details = details_for(rec.item, summary="### Po cichu zmienione")
        fake.reset_calls()
        assert (await syncer.run_once()).processed == 0  # within refresh window
        now[0] += timedelta(hours=2)
        assert (await syncer.run_once()).processed == 1
    assert "Po cichu" in (dirs["meta"] / REL / "summary.md").read_text(encoding="utf-8")
    assert fake.count("s3") == 0


async def test_one_failing_recording_does_not_stop_others(fake: FakePocket, syncer: Syncer, db):
    fake.add("r1")
    fake.add("bad")
    fake.add("r3")
    fake.details_failures["bad"] = [httpx.Response(400, json={"success": False, "error": "nope"})]
    stats = await syncer.run_once()
    assert (stats.status, stats.processed, stats.failed) == ("partial", 2, 1)
    bad = db.get_recording("bad")
    assert bad["error_count"] == 1 and "nope" in bad["last_error"]
    # next run retries it and resets the counter
    stats = await syncer.run_once()
    assert (stats.processed, stats.failed) == (1, 0)
    assert db.get_recording("bad")["error_count"] == 0


async def test_interrupted_download_is_completed_next_run(fake: FakePocket, syncer: Syncer, db, dirs):
    rec = fake.add("r1")
    truncated = httpx.Response(200, content=rec.audio[:100], headers={"Content-Length": str(len(rec.audio))})
    fake.s3_failures["r1"] = [truncated, truncated, truncated]
    stats = await syncer.run_once()
    assert stats.failed == 1
    audio_dir = dirs["audio"] / REL
    assert not (audio_dir / "audio.ogg").exists()
    assert not list(audio_dir.glob("*.part"))
    assert db.get_recording("r1")["audio_status"] == "failed"
    assert (dirs["meta"] / REL / "summary.md").exists()  # metadata still written

    stats = await syncer.run_once()
    assert (stats.processed, stats.failed) == (1, 0)
    assert (audio_dir / "audio.ogg").read_bytes() == rec.audio


async def test_expired_url_is_refreshed(fake: FakePocket, syncer: Syncer, dirs):
    rec = fake.add("r1")
    fake.s3_failures["r1"] = [httpx.Response(403, text="Request has expired")]
    stats = await syncer.run_once()
    assert stats.processed == 1
    assert fake.count("audio-url") == 2
    assert (dirs["audio"] / REL / "audio.ogg").read_bytes() == rec.audio


async def test_missing_audio_is_skipped_permanently(fake: FakePocket, syncer: Syncer, db):
    rec = fake.add("r1")
    rec.audio = None
    stats = await syncer.run_once()
    assert (stats.processed, stats.failed) == (1, 0)
    assert db.get_recording("r1")["audio_status"] == "skipped"
    fake.reset_calls()
    await syncer.run_once()
    assert fake.count("audio-url") == 0


async def test_mp3_extension_from_url(fake: FakePocket, syncer: Syncer, db, dirs):
    rec = fake.add("r1")
    rec.ext = ".mp3"
    await syncer.run_once()
    assert db.get_recording("r1")["audio_file"] == "audio.mp3"
    assert (dirs["audio"] / REL / "audio.mp3").exists()


async def test_validation_failure_still_writes_raw_json(fake: FakePocket, syncer: Syncer, db, dirs):
    rec = fake.add("r1")
    rec.details["transcript"] = "not an object"
    stats = await syncer.run_once()
    assert stats.failed == 1
    assert json.loads((dirs["meta"] / REL / "raw.json").read_text(encoding="utf-8"))["transcript"] == "not an object"
    row = db.get_recording("r1")
    assert row["meta_status"] == "failed"
    assert row["audio_status"] == "done"  # audio has priority and does not depend on the models


async def test_processing_recordings_are_deferred(fake: FakePocket, syncer: Syncer, db, dirs):
    fake.add("r1", state="processing", title="")
    stats = await syncer.run_once()
    assert (stats.processed, stats.skipped) == (0, 1)
    assert db.get_recording("r1") is None
    assert not dirs["meta"].exists() or not any(dirs["meta"].rglob("*untitled*"))


async def test_pending_summary_is_rechecked(fake: FakePocket, syncer: Syncer, db):
    rec = fake.add("r1")
    rec.details["summarizations"]["sum-1"]["processingStatus"] = "processing"
    await syncer.run_once()
    assert db.get_recording("r1")["meta_status"] == "pending"
    rec.details["summarizations"]["sum-1"]["processingStatus"] = "completed"
    fake.reset_calls()
    await syncer.run_once()
    assert fake.count("details") == 1 and fake.count("s3") == 0
    assert db.get_recording("r1")["meta_status"] == "done"


async def test_download_audio_false_never_touches_audio_dir(fake: FakePocket, make_settings, dirs):
    settings = make_settings(download_audio=False)
    fake.add("r1")
    with StateDB(settings.db_path) as db:
        db.migrate()
        async with PocketClient(BASE, "pk_test", sleep=no_sleep) as api:
            stats = await Syncer(settings, api, db).run_once()
        assert stats.status == "ok"
        assert db.get_recording("r1")["audio_status"] == "pending"
    assert not dirs["audio"].exists()
    assert fake.count("audio-url") == 0
    assert (dirs["meta"] / REL / "summary.md").exists()


async def test_trees_are_independent(fake: FakePocket, syncer: Syncer, dirs):
    fake.add("r1")
    await syncer.run_once()
    audio_files = [p for p in dirs["audio"].rglob("*") if p.is_file()]
    meta_files = [p for p in dirs["meta"].rglob("*") if p.is_file()]
    assert all(dirs["audio"] in p.parents for p in audio_files) and len(audio_files) == 1
    assert all(dirs["meta"] in p.parents for p in meta_files)
    assert not any(p.suffix == ".ogg" for p in meta_files)
    assert dirs["meta"].parents[1] != dirs["audio"].parent  # separate tmpdirs


async def test_error_backoff_after_repeated_failures(fake: FakePocket, make_settings, db):
    settings = make_settings()
    now = [datetime(2026, 9, 25, 10, 0, tzinfo=UTC)]
    fake.add("bad")
    fake.details_failures["bad"] = [httpx.Response(400, json={"success": False, "error": "x"})] * 20
    async with PocketClient(BASE, "pk_test", sleep=no_sleep) as api:
        syncer = Syncer(settings, api, db, clock=lambda: now[0])
        for _ in range(ATTENTION_THRESHOLD):
            await syncer.run_once()
        assert db.get_recording("bad")["error_count"] == ATTENTION_THRESHOLD
        fake.reset_calls()
        stats = await syncer.run_once()
        assert stats.skipped == 1 and fake.count("details") == 0
        now[0] += timedelta(minutes=16)
        await syncer.run_once()
        assert fake.count("details") == 1


# --- verify / healthcheck / CLI ---------------------------------------------------------------


async def test_verify_healthy_then_missing_audio(fake: FakePocket, syncer: Syncer, settings, dirs, capsys):
    fake.add("r1")
    await syncer.run_once()
    assert run_verify(settings, checksums=True) == 0
    (dirs["audio"] / REL / "audio.ogg").unlink()
    assert run_verify(settings) == 1
    out = capsys.readouterr().out
    assert "missing_audio" in out and "r1" in out


async def test_verify_detects_checksum_orphans_and_parts(fake: FakePocket, syncer: Syncer, settings, dirs):
    fake.add("r1")
    await syncer.run_once()
    (dirs["audio"] / REL / "audio.ogg").write_bytes(b"X" * len(fake.recordings["r1"].audio))
    orphan = dirs["audio"] / "2026/09/orphan"
    orphan.mkdir(parents=True)
    (orphan / "audio.ogg.part").write_bytes(b"x")
    problems, _ = verify(settings, checksums=True)
    kinds = {p.kind for p in problems}
    assert kinds == {"checksum_mismatch", "orphan_audio_dir", "partial_file"}


async def test_healthcheck(fake: FakePocket, syncer: Syncer, settings, db):
    assert healthcheck(settings) == 1  # migrated but never synced
    fake.add("r1")
    await syncer.run_once()
    assert healthcheck(settings) == 0
    old = iso(datetime.now(UTC) - timedelta(hours=2))
    db.conn.execute("UPDATE sync_runs SET finished_at = ?, heartbeat_at = ?", (old, old))
    assert healthcheck(settings) == 1


def test_missing_api_key_is_readable(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.delenv("POCKET_API_KEY", raising=False)
    monkeypatch.delenv("POCKET_API_KEY_FILE", raising=False)
    monkeypatch.setenv("META_DIR", str(tmp_path / "m"))
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("DOWNLOAD_AUDIO", "false")
    assert main(["once"]) == 2
    err = capsys.readouterr().err
    assert "POCKET_API_KEY is not set" in err
    assert "Traceback" not in err


def test_invalid_config_is_readable(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MAX_CONCURRENCY", "zero")
    assert main(["once"]) == 2
    assert "MAX_CONCURRENCY" in capsys.readouterr().err
