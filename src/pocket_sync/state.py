"""SQLite state: schema, migrations, queries."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RECORDING_COLUMNS = (
    "id",
    "created_at",
    "updated_at_remote",
    "title",
    "rel_dir",
    "meta_status",
    "meta_hash",
    "list_hash",
    "audio_status",
    "audio_file",
    "audio_sha256",
    "audio_bytes",
    "last_synced_at",
    "last_detail_fetch_at",
    "last_attempt_at",
    "error_count",
    "last_error",
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z") if dt else None


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _execute_all(conn: sqlite3.Connection, script: str) -> None:
    # Not executescript(): it COMMITs implicitly and would break the migration transaction.
    for statement in script.split(";"):
        if statement.strip():
            conn.execute(statement)


def _m1_initial(conn: sqlite3.Connection) -> None:
    _execute_all(
        conn,
        """
        CREATE TABLE recordings (
            id                   TEXT PRIMARY KEY,
            created_at           TEXT,
            updated_at_remote    TEXT,
            title                TEXT,
            rel_dir              TEXT NOT NULL,
            meta_status          TEXT NOT NULL DEFAULT 'pending',
            meta_hash            TEXT,
            list_hash            TEXT,
            audio_status         TEXT NOT NULL DEFAULT 'pending',
            audio_file           TEXT,
            audio_sha256         TEXT,
            audio_bytes          INTEGER,
            last_synced_at       TEXT,
            last_detail_fetch_at TEXT,
            last_attempt_at      TEXT,
            error_count          INTEGER NOT NULL DEFAULT 0,
            last_error           TEXT
        );
        CREATE TABLE sync_runs (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at       TEXT NOT NULL,
            finished_at      TEXT,
            heartbeat_at     TEXT,
            processed        INTEGER NOT NULL DEFAULT 0,
            skipped          INTEGER NOT NULL DEFAULT 0,
            failed           INTEGER NOT NULL DEFAULT 0,
            bytes_downloaded INTEGER NOT NULL DEFAULT 0,
            status           TEXT NOT NULL DEFAULT 'running',
            error            TEXT
        );
        CREATE INDEX sync_runs_status_finished ON sync_runs(status, finished_at);
        """
    )


# Append new migrations at the end; never edit or reorder existing ones.
MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [_m1_initial]


class StateDB:
    def __init__(self, path: Path, *, create: bool = True) -> None:
        if not create and not path.exists():
            raise FileNotFoundError(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, isolation_level=None, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=10000")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> StateDB:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- migrations -----------------------------------------------------------------------

    def schema_version(self) -> int:
        self.conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = self.conn.execute("SELECT version FROM schema_version").fetchone()
        return row["version"] if row else 0

    def migrate(self) -> int:
        current = self.schema_version()
        for version, migration in enumerate(MIGRATIONS[current:], start=current + 1):
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                migration(self.conn)
                self.conn.execute("DELETE FROM schema_version")
                self.conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
        return len(MIGRATIONS)

    # --- recordings -----------------------------------------------------------------------

    def all_recordings(self) -> dict[str, sqlite3.Row]:
        return {r["id"]: r for r in self.conn.execute("SELECT * FROM recordings")}

    def get_recording(self, recording_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM recordings WHERE id = ?", (recording_id,)).fetchone()

    def upsert_recording(self, recording_id: str, **fields: Any) -> None:
        unknown = set(fields) - set(RECORDING_COLUMNS)
        if unknown:
            raise ValueError(f"unknown columns: {sorted(unknown)}")
        cols = ["id", *fields]
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{c} = excluded.{c}" for c in fields) or "id = excluded.id"
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                f"INSERT INTO recordings ({', '.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}",
                (recording_id, *fields.values()),
            )
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def record_failure(self, recording_id: str, rel_dir: str, error: str, now: datetime) -> int:
        """Increment the consecutive error counter. Returns the new count."""
        row = self.get_recording(recording_id)
        count = (row["error_count"] if row else 0) + 1
        self.upsert_recording(
            recording_id,
            rel_dir=row["rel_dir"] if row else rel_dir,
            error_count=count,
            last_error=error[:2000],
            last_attempt_at=iso(now),
        )
        return count

    # --- sync runs ------------------------------------------------------------------------

    def start_run(self, now: datetime) -> int:
        # A run left in 'running' state was killed (SIGKILL, power loss).
        self.conn.execute(
            "UPDATE sync_runs SET status = 'aborted', finished_at = COALESCE(finished_at, ?) "
            "WHERE status = 'running'",
            (iso(now),),
        )
        cur = self.conn.execute(
            "INSERT INTO sync_runs (started_at, heartbeat_at, status) VALUES (?, ?, 'running')",
            (iso(now), iso(now)),
        )
        return int(cur.lastrowid)

    def heartbeat(self, run_id: int, now: datetime) -> None:
        self.conn.execute("UPDATE sync_runs SET heartbeat_at = ? WHERE id = ?", (iso(now), run_id))

    def finish_run(
        self,
        run_id: int,
        now: datetime,
        *,
        status: str,
        processed: int,
        skipped: int,
        failed: int,
        bytes_downloaded: int,
        error: str | None = None,
    ) -> None:
        self.conn.execute(
            "UPDATE sync_runs SET finished_at = ?, heartbeat_at = ?, status = ?, processed = ?, "
            "skipped = ?, failed = ?, bytes_downloaded = ?, error = ? WHERE id = ?",
            (iso(now), iso(now), status, processed, skipped, failed, bytes_downloaded, error, run_id),
        )

    def last_successful_run(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM sync_runs WHERE status IN ('ok', 'partial') "
            "ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()

    def running_run(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM sync_runs WHERE status = 'running' ORDER BY id DESC LIMIT 1"
        ).fetchone()
