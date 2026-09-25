"""Path layout shared by the metadata and audio trees.

Both trees use the same relative directory, computed only by `relative_dir()`. Nothing here
derives a path in one tree from a path in the other.
"""

from __future__ import annotations

import os
import re
import unicodedata
import uuid
from datetime import UTC, datetime, tzinfo
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

from .models import RecordingSummary

SLUG_MAX_LEN = 60
DEFAULT_AUDIO_EXT = ".bin"
KNOWN_AUDIO_EXTS = {".ogg", ".opus", ".mp3", ".m4a", ".mp4", ".aac", ".wav", ".flac", ".webm"}
CONTENT_TYPE_EXTS = {
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/flac": ".flac",
    "audio/webm": ".webm",
}

# Letters that NFKD does not decompose into ASCII base + combining mark.
_TRANSLIT = str.maketrans(
    {
        "ł": "l", "Ł": "L", "ß": "ss", "æ": "ae", "Æ": "AE", "ø": "o", "Ø": "O",
        "đ": "d", "Đ": "D", "þ": "th", "Þ": "TH", "œ": "oe", "Œ": "OE",
    }
)


class StorageError(Exception):
    """A storage root is missing or not writable. Fatal for the run."""


def slugify(title: str | None, max_len: int = SLUG_MAX_LEN) -> str:
    text = (title or "").translate(_TRANSLIT)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(text) > max_len:
        text = text[:max_len].rstrip("-")
    return text or "untitled"


def safe_id(recording_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", recording_id).strip("-") or "id"


def relative_dir(recording: RecordingSummary, tz: tzinfo = UTC) -> PurePosixPath:
    """`YYYY/MM/YYYY-MM-DD_HHMM_<slug>_<id>`: the single source of the per-recording path."""
    started = recording.started_at or datetime.fromtimestamp(0, UTC)
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    local = started.astimezone(tz)
    name = f"{local:%Y-%m-%d_%H%M}_{slugify(recording.title)}_{safe_id(recording.id)}"
    return PurePosixPath(f"{local:%Y}", f"{local:%m}", name)


def audio_extension(url: str, content_type: str | None = None) -> str:
    suffix = PurePosixPath(unquote(urlparse(url).path)).suffix.lower()
    if suffix in KNOWN_AUDIO_EXTS:
        return suffix
    if content_type:
        ext = CONTENT_TYPE_EXTS.get(content_type.split(";")[0].strip().lower())
        if ext:
            return ext
    return suffix if re.fullmatch(r"\.[a-z0-9]{1,5}", suffix) else DEFAULT_AUDIO_EXT


class Storage:
    """The two independent storage roots."""

    def __init__(self, meta_root: Path, audio_root: Path):
        self.meta_root = meta_root
        self.audio_root = audio_root

    def meta_dir(self, rel: PurePosixPath | str) -> Path:
        return self.meta_root / PurePosixPath(rel)

    def audio_dir(self, rel: PurePosixPath | str) -> Path:
        return self.audio_root / PurePosixPath(rel)

    @staticmethod
    def ensure_writable(root: Path, label: str) -> None:
        try:
            root.mkdir(parents=True, exist_ok=True)
            probe = root / f".write-test-{uuid.uuid4().hex}"
            with open(probe, "wb") as f:
                f.write(b"ok")
                f.flush()
                os.fsync(f.fileno())
            probe.unlink()
        except OSError as e:
            raise StorageError(f"{label} {root} is not writable: {e.strerror or e}") from e
