"""Entry point.

    python -m pocket_sync [run]               sync loop (or a single pass with RUN_ONCE=true)
    python -m pocket_sync once                a single pass, regardless of RUN_ONCE
    python -m pocket_sync verify [--checksums]
    python -m pocket_sync healthcheck         exit 0 if a sync succeeded recently (Docker HEALTHCHECK)
    python -m pocket_sync web                 web UI (needs WEB_PASSWORD)
    python -m pocket_sync web-healthcheck     exit 0 if the web UI answers (Docker HEALTHCHECK)
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
from .runtime import effective_settings, sync_requested, take_sync_request
from .state import StateDB, parse_iso, utcnow
from .sync import Syncer, check_storage
from .verify import run_verify

log = structlog.get_logger("pocket_sync")

EXIT_OK = 0
EXIT_PROBLEMS = 1
EXIT_CONFIG = 2
EXIT_STORAGE = 3

SYNC_POLL_SECONDS = 5  # how often the idle loop checks for "sync now" and interval changes


async def run_sync(settings: Settings, *, once: bool) -> int:
    api_key = settings.api_key()
    startup = effective_settings(settings)
    # Check storage before touching the DB so a missing mount fails loudly and early.
    check_storage(startup)
    async with PocketClient(settings.pocket_api_base, api_key) as api:
        with StateDB(settings.db_path) as db:
            db.migrate()
            log.info(
                "pocket_sync_started",
                version=__version__,
                mode="once" if once else "loop",
                interval_min=startup.sync_interval_minutes,
                download_audio=startup.download_audio,
                paused=startup.sync_paused,
                meta_dir=str(settings.meta_dir),
                audio_dir=str(settings.audio_dir) if startup.download_audio else None,
            )
            while True:
                forced = take_sync_request(settings.state_dir)
                current = effective_settings(settings)
                status = "failed"
                if current.sync_paused and not forced and not once:
                    log.info("sync_run_skipped", reason="paused")
                else:
                    try:
                        status = (await Syncer(current, api, db).run_once()).status
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
                await wait_for_next_run(settings)


async def wait_for_next_run(settings: Settings, *, poll_seconds: float = SYNC_POLL_SECONDS) -> None:
    """Sleep until the (runtime-editable) interval elapses or a "sync now" request appears."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    while not sync_requested(settings.state_dir):
        interval = effective_settings(settings).sync_interval_minutes * 60
        remaining = started + interval - loop.time()
        if remaining <= 0:
            return
        await asyncio.sleep(min(poll_seconds, remaining))


def healthcheck(settings: Settings) -> int:
    if not settings.db_path.exists():
        print("unhealthy: no state database yet")
        return EXIT_PROBLEMS
    settings = effective_settings(settings)
    if settings.sync_paused:
        print("healthy: sync is paused")
        return EXIT_OK
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


def run_web(settings: Settings) -> int:
    import uvicorn

    from .web import create_app

    app = create_app(settings)  # raises ConfigError without WEB_PASSWORD
    log.info("pocket_sync_web_started", version=__version__, host=settings.web_host, port=settings.web_port)
    # log_config=None: uvicorn's loggers go through the structlog handlers set up in setup_logging.
    uvicorn.run(
        app,
        host=settings.web_host,
        port=settings.web_port,
        log_config=None,
        proxy_headers=True,
        server_header=False,
    )
    return EXIT_OK


def web_healthcheck(settings: Settings) -> int:
    import httpx

    url = f"http://127.0.0.1:{settings.web_port}/healthz"
    try:
        response = httpx.get(url, timeout=10)
    except httpx.HTTPError as e:
        print(f"unhealthy: {url}: {e}")
        return EXIT_PROBLEMS
    if response.status_code != 200:
        print(f"unhealthy: {url} returned {response.status_code}")
        return EXIT_PROBLEMS
    print("healthy")
    return EXIT_OK


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
    sub.add_parser("web", help="web UI for browsing the archive and managing the sync")
    sub.add_parser("web-healthcheck", help="exit 0 if the web UI answers")
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
    if command == "web-healthcheck":
        return web_healthcheck(settings)

    setup_logging(settings)
    if command == "web":
        try:
            return run_web(settings)
        except ConfigError as e:
            log.error("config_error", error=str(e))
            print(f"pocket-sync: {e}", file=sys.stderr)
            return EXIT_CONFIG
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
