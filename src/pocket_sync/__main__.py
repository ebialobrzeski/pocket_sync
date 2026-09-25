"""Entry point.

    python -m pocket_sync [run]               sync loop (or a single pass with RUN_ONCE=true)
    python -m pocket_sync once                a single pass, regardless of RUN_ONCE
    python -m pocket_sync verify [--checksums]
    python -m pocket_sync healthcheck         exit 0 if a sync succeeded recently (Docker HEALTHCHECK)
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from datetime import timedelta

import structlog

from . import __version__
from .api import PermanentApiError, PocketClient
from .config import ConfigError, Settings, load_settings
from .logs import setup_logging
from .paths import StorageError
from .state import StateDB, parse_iso, utcnow
from .sync import Syncer, check_storage
from .verify import run_verify

log = structlog.get_logger("pocket_sync")

EXIT_OK = 0
EXIT_PROBLEMS = 1
EXIT_CONFIG = 2
EXIT_STORAGE = 3


async def run_sync(settings: Settings, *, once: bool) -> int:
    api_key = settings.api_key()
    # Check storage before touching the DB so a missing mount fails loudly and early.
    check_storage(settings)
    async with PocketClient(settings.pocket_api_base, api_key) as api:
        with StateDB(settings.db_path) as db:
            db.migrate()
            syncer = Syncer(settings, api, db)
            log.info(
                "pocket_sync_started",
                version=__version__,
                mode="once" if once else "loop",
                interval_min=settings.sync_interval_minutes,
                download_audio=settings.download_audio,
                meta_dir=str(settings.meta_dir),
                audio_dir=str(settings.audio_dir) if settings.download_audio else None,
            )
            while True:
                status = "failed"
                try:
                    status = (await syncer.run_once()).status
                except StorageError:
                    raise
                except PermanentApiError as e:
                    if e.status in (401, 403):
                        raise ConfigError(f"Pocket API rejected the API key: {e}") from None
                    log.error("sync_run_failed", error=str(e))
                except Exception as e:
                    log.exception("sync_run_failed", error=str(e))
                if once:
                    return EXIT_OK if status == "ok" else EXIT_PROBLEMS
                await asyncio.sleep(settings.sync_interval_minutes * 60)


def healthcheck(settings: Settings) -> int:
    if not settings.db_path.exists():
        print("unhealthy: no state database yet")
        return EXIT_PROBLEMS
    max_age = timedelta(minutes=3 * settings.sync_interval_minutes)
    now = utcnow()
    try:
        with StateDB(settings.db_path, create=False) as db:
            last = db.last_successful_run()
            running = db.running_run()
    except Exception as e:
        print(f"unhealthy: cannot read state: {e}")
        return EXIT_PROBLEMS
    if last and (finished := parse_iso(last["finished_at"])) and now - finished < max_age:
        print(f"healthy: last successful run finished {finished.isoformat()}")
        return EXIT_OK
    if running and (beat := parse_iso(running["heartbeat_at"])) and now - beat < max_age:
        print(f"healthy: run in progress, last progress {beat.isoformat()}")
        return EXIT_OK
    print(f"unhealthy: no successful run in the last {max_age}")
    return EXIT_PROBLEMS


async def _run_cancellable(settings: Settings, once: bool) -> int:
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    assert task is not None
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, task.cancel)
        except (NotImplementedError, RuntimeError):  # Windows
            pass
    try:
        return await run_sync(settings, once=once)
    except asyncio.CancelledError:
        log.info("pocket_sync_stopped", reason="signal")
        return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pocket-sync", description="Sync Pocket recordings to a NAS archive.")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="sync loop (default); honours RUN_ONCE")
    sub.add_parser("once", help="single sync pass")
    p_verify = sub.add_parser("verify", help="check archive consistency")
    p_verify.add_argument("--checksums", action="store_true", help="recompute SHA-256 of every audio file")
    sub.add_parser("healthcheck", help="exit 0 if a sync succeeded recently")
    args = parser.parse_args(argv)
    command = args.command or "run"

    try:
        settings = load_settings()
    except ConfigError as e:
        print(f"pocket-sync: {e}", file=sys.stderr)
        return EXIT_CONFIG

    if command == "healthcheck":
        return healthcheck(settings)
    if command == "verify":
        return run_verify(settings, checksums=args.checksums)

    setup_logging(settings)
    try:
        return asyncio.run(_run_cancellable(settings, once=command == "once" or settings.run_once))
    except ConfigError as e:
        log.error("config_error", error=str(e))
        print(f"pocket-sync: {e}", file=sys.stderr)
        return EXIT_CONFIG
    except StorageError as e:
        log.error("storage_error", error=str(e))
        print(f"pocket-sync: {e}", file=sys.stderr)
        return EXIT_STORAGE
    except KeyboardInterrupt:
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
