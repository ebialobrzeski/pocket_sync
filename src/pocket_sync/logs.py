"""structlog on top of stdlib logging: JSON or console to stdout, optional rotating file."""

from __future__ import annotations

import logging
import logging.handlers
import sys

import structlog

from .config import Settings


def setup_logging(settings: Settings | None = None) -> None:
    level = getattr(logging, settings.log_level if settings else "INFO")
    fmt = settings.log_format if settings else "console"

    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    def formatter(renderer: structlog.types.Processor) -> logging.Formatter:
        return structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.format_exc_info,
                renderer,
            ],
        )

    json_renderer = structlog.processors.JSONRenderer(ensure_ascii=False)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(
        formatter(json_renderer if fmt == "json" else structlog.dev.ConsoleRenderer(colors=False))
    )
    handlers: list[logging.Handler] = [stream]
    if settings and settings.log_to_file:
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            settings.log_dir / "pocket-sync.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(formatter(json_renderer))
        handlers.append(file_handler)

    root = logging.getLogger()
    root.handlers[:] = handlers
    root.setLevel(level)
    # httpx logs every request URL at INFO; presigned audio URLs carry credentials.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
