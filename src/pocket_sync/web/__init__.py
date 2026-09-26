"""Web UI for the local archive: `python -m pocket_sync web`."""

from .app import create_app

__all__ = ["create_app"]
