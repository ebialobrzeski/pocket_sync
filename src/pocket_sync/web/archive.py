"""Read access to the local archive (state DB + metadata and audio trees) for the web UI."""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any

import structlog
from markdown_it import MarkdownIt
from pydantic import ValidationError

from ..config import Settings
from ..models import RecordingDetails
from ..paths import _TRANSLIT
from ..state import StateDB, parse_iso

log = structlog.get_logger(__name__)

# Files of a recording's metadata directory that may be downloaded from the UI.
DOWNLOADABLE = ("summary.md", "transcript.md", "transcript.json", "actions.json", "raw.json", ".meta.json")
SNIPPET_RADIUS = 80

_md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False}).enable("table")


def render_markdown(text: str) -> str:
    """Markdown to HTML. Raw HTML in the source is escaped, unsafe link schemes are dropped."""
    return _md.render(text)


@lru_cache(maxsize=4096)
def _fold_char(ch: str) -> str:
    ch = ch.translate(_TRANSLIT)
    return "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c)).casefold()


def fold(text: str) -> str:
    """Case- and diacritic-insensitive form used for search ("Łódź" matches "lodz")."""
    return "".join(_fold_char(c) for c in text)


def _fold_with_map(text: str) -> tuple[str, list[int]]:
    """Folded text plus, for every folded character, its index in `text`."""
    out: list[str] = []
    index: list[int] = []
    for i, ch in enumerate(text):
        f = _fold_char(ch)
        out.append(f)
        index.extend([i] * len(f))
    return "".join(out), index


def snippet(text: str, terms: list[str], radius: int = SNIPPET_RADIUS) -> str | None:
    """A short excerpt of `text` around the first occurrence of any of `terms` (already folded)."""
    folded, index = _fold_with_map(text)
    hits = [pos for t in terms if (pos := folded.find(t)) >= 0]
    if not hits:
        return None
    start = index[min(hits)]
    lo, hi = max(0, start - radius), min(len(text), start + radius)
    excerpt = " ".join(text[lo:hi].split())
    return ("…" if lo > 0 else "") + excerpt + ("…" if hi < len(text) else "")


@dataclass
class ListItem:
    id: str
    title: str
    recorded_at: datetime | None
    duration: float | None
    folder: str | None
    tags: list[str]
    meta_status: str
    audio_status: str
    error_count: int
    snippet: str | None = None


@dataclass
class Summary:
    created_at: datetime | None
    html: str


@dataclass
class RecordingView:
    id: str
    title: str
    recorded_at: datetime | None
    duration: float | None
    folder: str | None
    tags: list[str]
    language: str | None
    row: sqlite3.Row
    meta: dict[str, Any]
    segments: list[dict[str, Any]] = field(default_factory=list)
    transcript_text: str | None = None
    summaries: list[Summary] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    files: list[tuple[str, int]] = field(default_factory=list)
    has_audio: bool = False
    problem: str | None = None


class Archive:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # rel_dir -> ((raw.json mtime, .meta.json mtime), info) for the recordings list
        self._info_cache: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}

    # --- state DB -------------------------------------------------------------------------

    @contextmanager
    def db(self) -> Iterator[StateDB | None]:
        """The state DB, or None while the sync has not created it yet."""
        try:
            db = StateDB(self.settings.db_path, create=False)
        except FileNotFoundError:
            yield None
            return
        try:
            yield db
        finally:
            db.close()

    def _rows(self) -> list[sqlite3.Row]:
        with self.db() as db:
            if db is None:
                return []
            try:
                return db.conn.execute(
                    "SELECT * FROM recordings ORDER BY created_at DESC, id DESC"
                ).fetchall()
            except sqlite3.OperationalError:  # tables not created yet
                return []

    def get_row(self, recording_id: str) -> sqlite3.Row | None:
        with self.db() as db:
            if db is None:
                return None
            try:
                return db.get_recording(recording_id)
            except sqlite3.OperationalError:
                return None

    # --- paths ----------------------------------------------------------------------------

    @staticmethod
    def _inside(root: Path, rel: str | PurePosixPath) -> Path | None:
        """`root/rel`, or None if it would escape `root`."""
        path = (root / PurePosixPath(rel)).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError:
            return None
        return path

    def meta_dir(self, row: sqlite3.Row) -> Path | None:
        return self._inside(self.settings.meta_dir, row["rel_dir"])

    def meta_file(self, row: sqlite3.Row, name: str) -> Path | None:
        if name not in DOWNLOADABLE or (d := self.meta_dir(row)) is None:
            return None
        path = d / name
        return path if path.is_file() else None

    def audio_path(self, row: sqlite3.Row) -> Path | None:
        if row["audio_status"] != "done" or not row["audio_file"]:
            return None
        path = self._inside(self.settings.audio_dir, PurePosixPath(row["rel_dir"]) / row["audio_file"])
        return path if path and path.is_file() else None

    @staticmethod
    def _read_json(path: Path | None) -> Any:
        if path is None:
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    @classmethod
    def _read_dict(cls, path: Path | None) -> dict[str, Any]:
        data = cls._read_json(path)
        return data if isinstance(data, dict) else {}

    # --- list -----------------------------------------------------------------------------

    def _info(self, row: sqlite3.Row) -> dict[str, Any]:
        """Duration, folder and tags of a recording, cached until its files change."""
        d = self.meta_dir(row)
        if d is None:
            return {}
        raw_p, meta_p = d / "raw.json", d / ".meta.json"

        def mtime(p: Path) -> int:
            try:
                return p.stat().st_mtime_ns
            except OSError:
                return 0

        key = (mtime(raw_p), mtime(meta_p))
        cached = self._info_cache.get(row["rel_dir"])
        if cached and cached[0] == key:
            return cached[1]
        raw = self._read_dict(raw_p)
        meta = self._read_dict(meta_p)
        info = {"duration": raw.get("duration"), "folder": meta.get("folder"), "tags": meta.get("tags") or []}
        self._info_cache[row["rel_dir"]] = (key, info)
        return info

    def _content_text(self, row: sqlite3.Row) -> str:
        d = self.meta_dir(row)
        if d is None:
            return ""
        parts: list[str] = []
        transcript = self._read_json(d / "transcript.json")
        if isinstance(transcript, dict):
            text = transcript.get("text") or " ".join(
                s.get("text", "") for s in transcript.get("segments") or [] if isinstance(s, dict)
            )
            parts.append(text or "")
        try:
            parts.append((d / "summary.md").read_text(encoding="utf-8"))
        except OSError:
            pass
        return "\n".join(parts)

    def list(
        self, query: str = "", *, content: bool = False, offset: int = 0, limit: int | None = None
    ) -> tuple[list[ListItem], int]:
        """A page of recordings, newest first, and the number of matches.

        Every word of `query` must appear in the title, or with `content` in the title,
        transcript or summary.
        """
        terms = fold(query).split()
        matches: list[tuple[sqlite3.Row, str | None]] = []
        for row in self._rows():
            if not terms:
                matches.append((row, None))
                continue
            folded_title = fold(row["title"] or "Untitled")
            if all(t in folded_title for t in terms):
                matches.append((row, None))
            elif content:
                text = self._content_text(row)
                haystack = folded_title + "\n" + fold(text)
                if all(t in haystack for t in terms):
                    matches.append((row, snippet(text, terms)))
        page = matches[offset : None if limit is None else offset + limit]
        items = []
        for row, snip in page:
            info = self._info(row)
            items.append(
                ListItem(
                    id=row["id"],
                    title=row["title"] or "Untitled",
                    recorded_at=parse_iso(row["created_at"]),
                    duration=info.get("duration"),
                    folder=info.get("folder"),
                    tags=info.get("tags") or [],
                    meta_status=row["meta_status"],
                    audio_status=row["audio_status"],
                    error_count=row["error_count"],
                    snippet=snip,
                )
            )
        return items, len(matches)

    # --- detail ---------------------------------------------------------------------------

    def recording(self, recording_id: str) -> RecordingView | None:
        row = self.get_row(recording_id)
        if row is None:
            return None
        d = self.meta_dir(row)
        meta = self._read_dict(d / ".meta.json" if d else None)
        view = RecordingView(
            id=row["id"],
            title=row["title"] or "Untitled",
            recorded_at=parse_iso(row["created_at"]),
            duration=None,
            folder=meta.get("folder"),
            tags=meta.get("tags") or [],
            language=None,
            row=row,
            meta=meta,
            has_audio=self.audio_path(row) is not None,
        )
        if d is not None:
            view.files = [(n, p.stat().st_size) for n in DOWNLOADABLE if (p := d / n).is_file()]

        raw = self._read_json(d / "raw.json" if d else None)
        if raw is None:
            view.problem = "raw.json is missing or unreadable; the next sync pass will fetch it again."
            return view
        try:
            details = RecordingDetails.model_validate(raw)
        except ValidationError as e:
            view.problem = f"raw.json could not be parsed: {e.errors()[0]['msg']}"
            return view

        view.duration = details.duration
        view.language = details.language
        if details.tags:
            view.tags = [t.name or t.id for t in details.tags]
        if details.transcript is not None:
            view.segments = [
                {"start": s.start or 0.0, "end": s.end, "speaker": s.speaker, "text": s.text.strip()}
                for s in details.transcript.segments
                if s.text.strip()
            ]
            view.transcript_text = details.transcript.text
        for _key, s in details.ordered_summarizations():
            if s.markdown:
                view.summaries.append(Summary(s.created_at, render_markdown(s.markdown)))
            view.actions.extend(s.actions)
        return view

    # --- sync status ----------------------------------------------------------------------

    def sync_status(self, runs: int = 15) -> dict[str, Any]:
        empty: dict[str, Any] = {"runs": [], "running": None, "attention": [], "counts": {}}
        with self.db() as db:
            if db is None:
                return empty
            try:
                recent = db.conn.execute(
                    "SELECT * FROM sync_runs ORDER BY id DESC LIMIT ?", (runs,)
                ).fetchall()
                attention = db.conn.execute(
                    "SELECT id, title, error_count, last_error, last_attempt_at FROM recordings "
                    "WHERE error_count > 0 ORDER BY error_count DESC, last_attempt_at DESC"
                ).fetchall()
                counts = db.conn.execute(
                    "SELECT COUNT(*) AS total, "
                    "SUM(audio_status = 'done') AS audio_done, "
                    "SUM(audio_status = 'failed') AS audio_failed, "
                    "SUM(meta_status = 'done') AS meta_done, "
                    "COALESCE(SUM(CASE WHEN audio_status = 'done' THEN audio_bytes END), 0) AS audio_bytes "
                    "FROM recordings"
                ).fetchone()
                running = db.running_run()
                last_ok = db.last_successful_run()
            except sqlite3.OperationalError:
                return empty
        return {
            "runs": recent,
            "running": running,
            "last_ok": last_ok,
            "attention": attention,
            "counts": dict(counts) if counts else {},
        }

    def resync(self, recording_id: str) -> bool:
        """Make the next sync pass re-fetch this recording, clearing its error backoff."""
        with self.db() as db:
            if db is None or (row := db.get_recording(recording_id)) is None:
                return False
            db.upsert_recording(
                recording_id, rel_dir=row["rel_dir"], list_hash=None, error_count=0, last_error=None
            )
        return True
