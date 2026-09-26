"""Sync settings editable at runtime (from the web UI) and the "sync now" request.

Both live in STATE_DIR, which the sync loop and the web UI share:

    settings.json   overrides of EDITABLE settings; they take precedence over the environment
    sync-now        present while a manual sync pass is requested

The sync loop re-reads both between passes, so changes apply without a restart.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import structlog
from pydantic import TypeAdapter, ValidationError

from .config import Settings
from .writer import write_json_atomic

log = structlog.get_logger(__name__)

EDITABLE = (
    "sync_paused",
    "download_audio",
    "sync_interval_minutes",
    "max_concurrency",
    "full_refresh_hours",
)

OVERRIDES_FILE = "settings.json"
SYNC_REQUEST_FILE = "sync-now"


def _adapter(name: str) -> TypeAdapter[Any]:
    # Reuse the type and constraints (gt/ge/le) declared on Settings.
    field = Settings.model_fields[name]
    if not field.metadata:
        return TypeAdapter(field.annotation)
    return TypeAdapter(Annotated[field.annotation, *field.metadata])


def validate_value(name: str, value: Any) -> Any:
    """Validate one editable setting. Raises ValueError with a readable message."""
    if name not in EDITABLE:
        raise ValueError(f"{name} cannot be changed at runtime")
    try:
        return _adapter(name).validate_python(value)
    except ValidationError as e:
        raise ValueError(e.errors()[0]["msg"]) from None


def load_overrides(state_dir: Path) -> dict[str, Any]:
    """Valid overrides from settings.json; unknown or invalid entries are ignored with a warning."""
    path = state_dir / OVERRIDES_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning("settings_overrides_unreadable", path=str(path), error=str(e))
        return {}
    if not isinstance(data, dict):
        log.warning("settings_overrides_unreadable", path=str(path), error="not a JSON object")
        return {}
    out: dict[str, Any] = {}
    for name, value in data.items():
        try:
            out[name] = validate_value(name, value)
        except ValueError as e:
            log.warning("settings_override_ignored", setting=name, error=str(e))
    return out


def save_overrides(state_dir: Path, overrides: dict[str, Any]) -> None:
    clean = {name: validate_value(name, value) for name, value in overrides.items()}
    state_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(state_dir / OVERRIDES_FILE, clean)


def effective_settings(settings: Settings) -> Settings:
    """`settings` (from the environment) with the runtime overrides applied."""
    overrides = load_overrides(settings.state_dir)
    return settings.model_copy(update=overrides) if overrides else settings


def request_sync(state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / SYNC_REQUEST_FILE).touch()


def sync_requested(state_dir: Path) -> bool:
    return (state_dir / SYNC_REQUEST_FILE).exists()


def take_sync_request(state_dir: Path) -> bool:
    """Consume a pending "sync now" request. Returns True when there was one."""
    try:
        (state_dir / SYNC_REQUEST_FILE).unlink()
    except FileNotFoundError:
        return False
    return True
