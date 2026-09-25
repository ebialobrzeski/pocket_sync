"""Orchestration of a single sync run."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

import httpx
import structlog
from pydantic import ValidationError

from . import __version__, render
from .api import AudioNotAvailable, PocketClient, TransientApiError
from .config import Settings
from .models import RecordingDetails, RecordingSummary, flatten_folders
from .paths import Storage, audio_extension, relative_dir
from .state import StateDB, iso, parse_iso, utcnow
from .writer import (
    PartFile,
    SizeMismatch,
    canonical_hash,
    cleanup_stale_parts,
    dumps_json,
    write_json_atomic,
    write_text_atomic,
)

log = structlog.get_logger(__name__)

AUDIO_ATTEMPTS = 3
ATTENTION_THRESHOLD = 5  # consecutive failures before a recording is flagged and backed off
MAX_BACKOFF = timedelta(hours=24)
AUDIO_DONE_STATES = {"done", "skipped"}


@dataclass
class RunStats:
    processed: int = 0
    skipped: int = 0
    failed: int = 0
    bytes_downloaded: int = 0
    status: str = "running"


@dataclass
class AudioResult:
    file: str
    sha256: str
    size: int
    content_type: str | None


def check_storage(settings: Settings) -> None:
    """Both trees must be writable; AUDIO_DIR is not touched at all when DOWNLOAD_AUDIO=false."""
    Storage.ensure_writable(settings.meta_dir, "META_DIR")
    Storage.ensure_writable(settings.state_dir, "STATE_DIR")
    if settings.download_audio:
        Storage.ensure_writable(settings.audio_dir, "AUDIO_DIR")


class Syncer:
    def __init__(
        self,
        settings: Settings,
        api: PocketClient,
        db: StateDB,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.settings = settings
        self.api = api
        self.db = db
        self.clock = clock
        self.tz = settings.zone
        self.storage = Storage(settings.meta_dir, settings.audio_dir)

    # --- run ------------------------------------------------------------------------------

    async def run_once(self) -> RunStats:
        check_storage(self.settings)
        if self.settings.download_audio:
            for p in cleanup_stale_parts(self.settings.audio_dir):
                log.warning("stale_part_removed", path=str(p))

        stats = RunStats()
        started = self.clock()
        run_id = self.db.start_run(started)
        error: str | None = None
        stats.status = "failed"
        try:
            recordings = await self.api.list_recordings()
            folders = await self._load_folders()
            rows = self.db.all_recordings()
            queue: list[tuple[RecordingSummary, sqlite3.Row | None, str]] = []
            for rec in recordings:
                row = rows.get(rec.id)
                reason, skip_reason = self._decide(rec, row, started)
                if reason:
                    queue.append((rec, row, reason))
                else:
                    stats.skipped += 1
                    log.debug("recording_skipped", recording_id=rec.id, reason=skip_reason)
            remote_ids = {r.id for r in recordings}
            local_only = sum(1 for rid in rows if rid not in remote_ids)
            log.info(
                "sync_plan",
                remote=len(recordings),
                queued=len(queue),
                skipped=stats.skipped,
                local_only=local_only,
            )

            sem = asyncio.Semaphore(self.settings.max_concurrency)

            async def worker(rec: RecordingSummary, row: sqlite3.Row | None, reason: str) -> None:
                async with sem:
                    await self._process(rec, row, reason, folders, stats)
                    self.db.heartbeat(run_id, self.clock())

            await asyncio.gather(*(worker(*item) for item in queue))
            stats.status = "ok" if stats.failed == 0 else "partial"
        except asyncio.CancelledError:
            stats.status = "aborted"
            raise
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            raise
        finally:
            finished = self.clock()
            self.db.finish_run(
                run_id,
                finished,
                status=stats.status,
                processed=stats.processed,
                skipped=stats.skipped,
                failed=stats.failed,
                bytes_downloaded=stats.bytes_downloaded,
                error=error,
            )
            log.info(
                "sync_run_finished",
                status=stats.status,
                processed=stats.processed,
                skipped=stats.skipped,
                failed=stats.failed,
                bytes_downloaded=stats.bytes_downloaded,
                duration_s=round((finished - started).total_seconds(), 1),
                error=error,
            )
        return stats

    async def _load_folders(self) -> dict[str, str]:
        try:
            return flatten_folders(await self.api.list_folders())
        except Exception as e:  # folder names are cosmetic; never fail the run over them
            log.warning("folders_unavailable", error=str(e))
            return {}

    # --- planning -------------------------------------------------------------------------

    @staticmethod
    def list_hash(rec: RecordingSummary) -> str:
        return canonical_hash(rec.model_dump(mode="json"))

    def _decide(
        self, rec: RecordingSummary, row: sqlite3.Row | None, now: datetime
    ) -> tuple[str | None, str | None]:
        """Return (queue_reason, None) or (None, skip_reason)."""
        if not rec.is_completed:
            return None, f"remote_state_{rec.state}"
        if row is None:
            return "new", None
        if row["error_count"] >= ATTENTION_THRESHOLD:
            last = parse_iso(row["last_attempt_at"])
            interval = timedelta(minutes=self.settings.sync_interval_minutes)
            delay = min(interval * 2 ** (row["error_count"] - ATTENTION_THRESHOLD), MAX_BACKOFF)
            if last and now - last < delay:
                return None, "error_backoff"
        if row["list_hash"] != self.list_hash(rec):
            return "changed", None
        if row["meta_status"] != "done":
            return f"meta_{row['meta_status']}", None
        if self.settings.download_audio and row["audio_status"] not in AUDIO_DONE_STATES:
            return f"audio_{row['audio_status']}", None
        if self.settings.full_refresh_hours > 0:
            fetched = parse_iso(row["last_detail_fetch_at"])
            if not fetched or now - fetched >= timedelta(hours=self.settings.full_refresh_hours):
                return "periodic_refresh", None
        return None, "up_to_date"

    # --- per recording --------------------------------------------------------------------

    async def _process(
        self,
        rec: RecordingSummary,
        row: sqlite3.Row | None,
        reason: str,
        folders: dict[str, str],
        stats: RunStats,
    ) -> None:
        rel = PurePosixPath(row["rel_dir"]) if row and row["rel_dir"] else relative_dir(rec, self.tz)
        blog = log.bind(recording_id=rec.id, rel_dir=rel.as_posix())
        try:
            await self._process_inner(rec, row, rel, reason, folders, stats, blog)
        except Exception as e:
            count = self.db.record_failure(rec.id, rel.as_posix(), f"{type(e).__name__}: {e}", self.clock())
            stats.failed += 1
            blog.error("recording_failed", error=str(e), error_type=type(e).__name__, error_count=count)
            if count >= ATTENTION_THRESHOLD:
                blog.warning("recording_needs_attention", error_count=count)

    async def _process_inner(
        self,
        rec: RecordingSummary,
        row: sqlite3.Row | None,
        rel: PurePosixPath,
        reason: str,
        folders: dict[str, str],
        stats: RunStats,
        blog: Any,
    ) -> None:
        raw = await self.api.get_recording(rec.id)
        now = self.clock()
        meta_dir = self.storage.meta_dir(rel)
        # raw.json first and unconditionally: it is the source of truth for everything derived.
        write_json_atomic(meta_dir / "raw.json", raw)
        meta_hash = canonical_hash(raw)
        errors: list[str] = []

        audio_status = row["audio_status"] if row else "pending"
        audio_file = row["audio_file"] if row else None
        audio_sha = row["audio_sha256"] if row else None
        audio_bytes = row["audio_bytes"] if row else None
        audio_ctype: str | None = None
        if self.settings.download_audio and audio_status not in AUDIO_DONE_STATES:
            try:
                res = await self._download_audio(rec.id, rel, blog)
            except AudioNotAvailable as e:
                audio_status = "skipped"
                blog.info("audio_not_available", error=str(e))
            except Exception as e:
                audio_status = "failed"
                errors.append(f"audio: {type(e).__name__}: {e}")
            else:
                audio_status, audio_file, audio_sha, audio_bytes = "done", res.file, res.sha256, res.size
                audio_ctype = res.content_type
                stats.bytes_downloaded += res.size
                blog.info("audio_downloaded", bytes=res.size, file=res.file)

        folder = folders.get(rec.folder_id or "")
        files: dict[str, str] = {}
        try:
            details = RecordingDetails.model_validate(raw)
            files = self._write_derived(meta_dir, details, folder)
            meta_status = "done" if details.is_processing_complete else "pending"
        except ValidationError as e:
            meta_status = "failed"
            errors.append(f"meta: validation failed: {e.error_count()} error(s): {e.errors()[0]['msg']}")
        except Exception as e:
            meta_status = "failed"
            errors.append(f"meta: {type(e).__name__}: {e}")

        # Relative to the audio share root, so it stays valid wherever the share is mounted.
        audio_rel_path = (
            (rel / audio_file).as_posix() if audio_status == "done" and audio_file else None
        )
        self._write_meta_json(
            meta_dir,
            {
                "tool": "pocket-sync",
                "tool_version": __version__,
                "recording_id": rec.id,
                "title": rec.title,
                "recorded_at": iso(rec.started_at),
                "updated_at_remote": iso(rec.updated_at),
                "folder": folder,
                "tags": [t.name or t.id for t in rec.tags],
                "rel_dir": rel.as_posix(),
                "meta_status": meta_status,
                "meta_hash": meta_hash,
                "audio_status": audio_status,
                "audio_rel_path": audio_rel_path,
                "audio_file": audio_file if audio_status == "done" else None,
                "audio_sha256": audio_sha if audio_status == "done" else None,
                "audio_bytes": audio_bytes if audio_status == "done" else None,
                "files": files,
            },
            now,
        )

        prev_errors = row["error_count"] if row else 0
        error_count = prev_errors + 1 if errors else 0
        self.db.upsert_recording(
            rec.id,
            created_at=iso(rec.started_at),
            updated_at_remote=iso(rec.updated_at),
            title=rec.title,
            rel_dir=rel.as_posix(),
            meta_status=meta_status,
            meta_hash=meta_hash,
            list_hash=self.list_hash(rec),
            audio_status=audio_status,
            audio_file=audio_file,
            audio_sha256=audio_sha,
            audio_bytes=audio_bytes,
            last_synced_at=iso(now),
            last_detail_fetch_at=iso(now),
            last_attempt_at=iso(now),
            error_count=error_count,
            last_error="; ".join(errors)[:2000] if errors else None,
        )
        if errors:
            stats.failed += 1
            blog.error("recording_failed", errors=errors, error_count=error_count)
            if error_count >= ATTENTION_THRESHOLD:
                blog.warning("recording_needs_attention", error_count=error_count)
        else:
            stats.processed += 1
            blog.info(
                "recording_synced",
                reason=reason,
                meta_status=meta_status,
                meta_changed=(row["meta_hash"] if row else None) != meta_hash,
                audio_status=audio_status,
                content_type=audio_ctype,
            )

    async def _download_audio(self, recording_id: str, rel: PurePosixPath, blog: Any) -> AudioResult:
        last_exc: Exception | None = None
        for attempt in range(1, AUDIO_ATTEMPTS + 1):
            # Presigned URLs expire: always fetch a fresh one right before the transfer.
            url = await self.api.get_audio_url(recording_id)
            final = self.storage.audio_dir(rel) / f"audio{audio_extension(url.signed_url)}"
            part = PartFile(final)
            try:
                ctype = await self.api.download(url.signed_url, part)
            except (TransientApiError, httpx.TransportError, SizeMismatch) as e:
                part.abort()
                last_exc = e
                blog.warning("audio_retry", attempt=attempt, error=f"{type(e).__name__}: {e}")
                continue
            except BaseException:
                part.abort()
                raise
            assert part.sha256 is not None
            return AudioResult(final.name, part.sha256, part.size, ctype)
        assert last_exc is not None
        raise last_exc

    def _write_derived(self, meta_dir: Path, rec: RecordingDetails, folder: str | None) -> dict[str, str]:
        outputs: dict[str, str | None] = {
            "transcript.json": (
                dumps_json(t) if (t := render.transcript_json(rec)) is not None else None
            ),
            "transcript.md": render.transcript_md(rec, self.tz, folder),
            "summary.md": render.summary_md(rec, self.tz, folder),
            "actions.json": dumps_json(render.actions_json(rec)),
        }
        hashes: dict[str, str] = {}
        for name, content in outputs.items():
            if content is None:
                continue
            write_text_atomic(meta_dir / name, content)
            hashes[name] = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return hashes

    @staticmethod
    def _write_meta_json(meta_dir: Path, meta: dict[str, Any], now: datetime) -> None:
        path = meta_dir / ".meta.json"
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            existing.pop("synced_at", None)
            if existing == json.loads(dumps_json(meta)):
                return  # unchanged: keep the file (and its synced_at) untouched
        except (OSError, ValueError):
            pass
        write_json_atomic(path, {**meta, "synced_at": iso(now)})
