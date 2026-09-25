"""Settings loaded from environment variables (and `.env` in the working directory, if any)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(Exception):
    """Configuration problem that should be reported to the user without a traceback."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    pocket_api_key: SecretStr | None = None
    pocket_api_key_file: Path | None = None  # Docker secret, e.g. /run/secrets/pocket_api_key
    pocket_api_base: str = "https://public.heypocketai.com/api/v1"

    meta_dir: Path = Path("/data/meta/pocket")
    state_dir: Path = Path("/data/state")
    audio_dir: Path = Path("/audio")

    download_audio: bool = True

    sync_interval_minutes: float = Field(15, gt=0)
    run_once: bool = False
    max_concurrency: int = Field(3, ge=1, le=20)
    # Re-fetch details of every recording this often to catch summary changes that do not bump
    # `updated_at`. 0 disables.
    full_refresh_hours: float = Field(24, ge=0)
    tz: str = "UTC"  # timezone used for directory names (YYYY/MM/YYYY-MM-DD_HHMM_...)

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "json"
    log_to_file: bool = False

    @field_validator("log_level", "log_format", mode="before")
    @classmethod
    def _normalize_case(cls, v: object) -> object:
        if not isinstance(v, str):
            return v
        return v.upper() if v.lower() in {"debug", "info", "warning", "error"} else v.lower()

    @field_validator("tz")
    @classmethod
    def _valid_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"unknown timezone {v!r}") from e
        return v

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def db_path(self) -> Path:
        return self.state_dir / "pocket-sync.db"

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "logs"

    def api_key(self) -> str:
        """Return the API key or raise ConfigError with a readable message."""
        key = self.pocket_api_key.get_secret_value().strip() if self.pocket_api_key else ""
        if not key and self.pocket_api_key_file:
            try:
                key = self.pocket_api_key_file.read_text(encoding="utf-8").strip()
            except OSError as e:
                raise ConfigError(
                    f"cannot read POCKET_API_KEY_FILE {self.pocket_api_key_file}: {e.strerror}"
                ) from e
        if not key:
            raise ConfigError(
                "POCKET_API_KEY is not set. Put it in .env (see .env.example) "
                "or point POCKET_API_KEY_FILE at a Docker secret."
            )
        if not key.startswith("pk_"):
            raise ConfigError("POCKET_API_KEY looks invalid: expected a key starting with 'pk_'.")
        return key


def load_settings(**overrides: object) -> Settings:
    try:
        return Settings(**overrides)  # type: ignore[arg-type]
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']).upper() or 'config'}: {err['msg']}"
            for err in e.errors()
        )
        raise ConfigError(f"invalid configuration: {problems}") from None
