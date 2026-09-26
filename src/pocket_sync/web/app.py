"""Web UI: browse recordings, listen to audio, read transcripts and summaries, manage the sync.

Server-rendered (FastAPI + Jinja2), protected by a single master password (WEB_PASSWORD).
"""

from __future__ import annotations

import hashlib
import hmac
import mimetypes
import secrets
import time
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import structlog
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from .. import __version__
from ..config import Settings
from ..render import fmt_ts
from ..runtime import (
    EDITABLE,
    effective_settings,
    load_overrides,
    request_sync,
    save_overrides,
    sync_requested,
    validate_value,
)
from ..state import parse_iso, utcnow
from .archive import Archive

log = structlog.get_logger(__name__)

HERE = Path(__file__).parent
PAGE_SIZE = 100
LOGIN_WINDOW = 15 * 60  # seconds
LOGIN_MAX_FAILURES = 10  # per client within LOGIN_WINDOW
FORM_SETTINGS = ("download_audio", "sync_interval_minutes", "max_concurrency", "full_refresh_hours")
CHECKBOXES = {"download_audio"}

CSP = (
    "default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self'; "
    "script-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


class LoginRequired(Exception):
    pass


class LoginThrottle:
    """Refuses logins from a client after too many recent failures."""

    def __init__(self, max_failures: int = LOGIN_MAX_FAILURES, window: float = LOGIN_WINDOW) -> None:
        self.max_failures = max_failures
        self.window = window
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _recent(self, client: str) -> deque[float]:
        q = self._failures[client]
        cutoff = time.monotonic() - self.window
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def blocked(self, client: str) -> bool:
        return len(self._recent(client)) >= self.max_failures

    def failed(self, client: str) -> None:
        self._recent(client).append(time.monotonic())

    def succeeded(self, client: str) -> None:
        self._failures.pop(client, None)


def _safe_next(target: str | None) -> str:
    """Only allow local absolute paths as redirect targets."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


def _local(settings: Settings, value: datetime | str | None) -> datetime | None:
    dt = parse_iso(value) if isinstance(value, str) else value
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(settings.zone)


def create_app(settings: Settings) -> FastAPI:
    password = settings.web_password_value()
    secret = (
        settings.web_secret_key.get_secret_value()
        if settings.web_secret_key
        else hmac.new(password.encode(), b"pocket-sync web session", hashlib.sha256).hexdigest()
    )
    # Stored in the session: changing the password invalidates existing sessions.
    password_tag = hmac.new(secret.encode(), password.encode(), hashlib.sha256).hexdigest()[:32]
    password_digest = hashlib.sha256(password.encode()).digest()

    archive = Archive(settings)
    throttle = LoginThrottle()
    templates = Jinja2Templates(directory=HERE / "templates")

    # --- template helpers -----------------------------------------------------------------

    def fmt_dt(value: datetime | str | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
        try:
            dt = _local(settings, value)
        except ValueError:  # unexpected format from the API: show it as is
            return str(value)
        return dt.strftime(fmt) if dt else "—"

    def fmt_num(value: Any) -> str:
        if isinstance(value, bool):
            return "on" if value else "off"
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)

    def fmt_length(seconds: float | None) -> str:
        """Human-readable length: "45 s", "12 min", "1 h 5 min"."""
        if seconds is None:
            return "—"
        secs = max(0, round(seconds))
        if secs < 60:
            return f"{secs} s"
        h, m = divmod(round(secs / 60), 60)
        return f"{h} h {m} min" if h else f"{m} min"

    def run_duration(run: Any) -> str:
        started, finished = parse_iso(run["started_at"]), parse_iso(run["finished_at"])
        if not started or not finished:
            return "—"
        return fmt_length((finished - started).total_seconds())

    def fmt_ago(value: datetime | str | None) -> str:
        dt = parse_iso(value) if isinstance(value, str) else value
        if dt is None:
            return "never"
        secs = int((utcnow() - dt).total_seconds())
        if abs(secs) < 60:
            return "just now"
        unit, size = next((u, n) for u, n in (("d", 86400), ("h", 3600), ("min", 60)) if abs(secs) >= n)
        amount = f"{abs(secs) // size} {unit}"
        return f"{amount} ago" if secs > 0 else f"in {amount}"

    def fmt_size(n: int | None) -> str:
        size = float(n or 0)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"

    templates.env.filters["dt"] = fmt_dt
    templates.env.filters["ago"] = fmt_ago
    templates.env.filters["timestamp"] = fmt_ts
    templates.env.filters["span"] = fmt_length
    templates.env.filters["size"] = fmt_size
    templates.env.filters["num"] = fmt_num
    templates.env.filters["run_duration"] = run_duration
    templates.env.globals["version"] = __version__

    def csrf_token(request: Request) -> str:
        token = request.session.get("csrf")
        if not token:
            token = request.session["csrf"] = secrets.token_urlsafe(32)
        return token

    def flash(request: Request, message: str, kind: str = "info") -> None:
        # Reassign rather than append: the session is only saved when a key is set.
        request.session["flash"] = [*request.session.get("flash", []), [kind, message]]

    def render(request: Request, name: str, status_code: int = 200, **context: Any) -> HTMLResponse:
        messages = request.session.pop("flash", [])
        return templates.TemplateResponse(
            request,
            name,
            {"csrf": csrf_token(request), "messages": messages, "tz": settings.tz, **context},
            status_code=status_code,
        )

    # --- dependencies ---------------------------------------------------------------------

    def require_login(request: Request) -> None:
        if not hmac.compare_digest(request.session.get("auth", ""), password_tag):
            raise LoginRequired()

    async def verify_csrf(request: Request) -> None:
        form = await request.form()
        sent = str(form.get("csrf", ""))
        expected = request.session.get("csrf", "")
        if not expected or not hmac.compare_digest(sent, expected):
            raise HTTPException(400, "Invalid or expired form. Go back, reload the page and try again.")

    authed = [Depends(require_login)]
    posted = [Depends(require_login), Depends(verify_csrf)]

    # --- app ------------------------------------------------------------------------------

    app = FastAPI(title="pocket-sync", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.archive = archive
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        session_cookie="pocket_sync_session",
        max_age=int(settings.web_session_hours * 3600),
        same_site="lax",
        https_only=settings.web_cookie_secure,
    )
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if not request.url.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(LoginRequired)
    async def _login_redirect(request: Request, _exc: LoginRequired) -> Response:
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target)}", status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if request.session.get("auth") and "text/html" in request.headers.get("accept", ""):
            return render(request, "error.html", status_code=exc.status_code, error=exc.detail)
        return PlainTextResponse(str(exc.detail), status_code=exc.status_code)

    # --- auth -----------------------------------------------------------------------------

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.get("/login")
    async def login_form(request: Request, next: str = "/") -> Response:
        if hmac.compare_digest(request.session.get("auth", ""), password_tag):
            return RedirectResponse(_safe_next(next), status_code=303)
        return render(request, "login.html", next=_safe_next(next))

    @app.post("/login", dependencies=[Depends(verify_csrf)])
    async def login(request: Request) -> Response:
        form = await request.form()
        target = _safe_next(str(form.get("next", "/")))
        client = request.client.host if request.client else "unknown"
        if throttle.blocked(client):
            log.warning("web_login_throttled", client=client)
            return render(
                request, "login.html", status_code=429, next=target,
                error="Too many failed attempts. Try again in a few minutes.",
            )
        sent = hashlib.sha256(str(form.get("password", "")).encode()).digest()
        if not hmac.compare_digest(sent, password_digest):
            throttle.failed(client)
            log.warning("web_login_failed", client=client)
            return render(request, "login.html", status_code=401, next=target, error="Wrong password.")
        throttle.succeeded(client)
        request.session.clear()
        request.session["auth"] = password_tag
        log.info("web_login", client=client)
        return RedirectResponse(target, status_code=303)

    @app.post("/logout", dependencies=[Depends(verify_csrf)])
    async def logout(request: Request) -> Response:
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # --- recordings -----------------------------------------------------------------------

    @app.get("/", dependencies=authed)
    async def recordings(request: Request, q: str = "", content: bool = False, page: int = 1) -> Response:
        page = max(1, page)
        items, total = archive.list(q, content=content, offset=(page - 1) * PAGE_SIZE, limit=PAGE_SIZE)
        groups: list[tuple[str, list[Any]]] = []
        for item in items:
            label = fmt_dt(item.recorded_at, "%B %Y") if item.recorded_at else "Unknown date"
            if not groups or groups[-1][0] != label:
                groups.append((label, []))
            groups[-1][1].append(item)
        pages = max(1, -(-total // PAGE_SIZE))

        def page_url(n: int) -> str:
            params: dict[str, Any] = {"page": n}
            if q:
                params["q"] = q
            if content:
                params["content"] = "true"
            return "/?" + urlencode(params)

        return render(
            request,
            "recordings.html",
            groups=groups,
            total=total,
            q=q,
            content=content,
            page=page,
            pages=pages,
            prev_url=page_url(page - 1) if page > 1 else None,
            next_url=page_url(page + 1) if page < pages else None,
            db_missing=not settings.db_path.exists(),
        )

    @app.get("/recordings/{recording_id}", dependencies=authed)
    async def recording(request: Request, recording_id: str) -> Response:
        view = archive.recording(recording_id)
        if view is None:
            raise HTTPException(404, "Recording not found.")
        return render(request, "recording.html", rec=view)

    @app.get("/recordings/{recording_id}/audio", dependencies=authed)
    async def audio(recording_id: str) -> Response:
        row = archive.get_row(recording_id)
        path = archive.audio_path(row) if row else None
        if path is None:
            raise HTTPException(404, "Audio file not found.")
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix == ".opus":
            media_type = "audio/ogg"
        # FileResponse answers Range requests, so the player can seek.
        return FileResponse(path, media_type=media_type, headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/recordings/{recording_id}/files/{name}", dependencies=authed)
    async def meta_file(recording_id: str, name: str) -> Response:
        row = archive.get_row(recording_id)
        path = archive.meta_file(row, name) if row else None
        if path is None:
            raise HTTPException(404, "File not found.")
        media_type = "application/json" if name.endswith(".json") else "text/plain"
        return FileResponse(path, media_type=f"{media_type}; charset=utf-8")

    @app.post("/recordings/{recording_id}/resync", dependencies=posted)
    async def resync(request: Request, recording_id: str) -> Response:
        form = await request.form()
        if archive.resync(recording_id):
            request_sync(settings.state_dir)
            flash(request, "Recording queued: it will be fetched again in the next sync pass.", "ok")
        else:
            flash(request, "Recording not found.", "error")
        return RedirectResponse(_safe_next(str(form.get("next", ""))), status_code=303)

    # --- settings -------------------------------------------------------------------------

    def status_context() -> dict[str, Any]:
        current = effective_settings(settings)
        status = archive.sync_status()
        running = status.get("running")
        stale = False
        if running and (beat := parse_iso(running["heartbeat_at"])):
            # No progress for a long time: the sync container is probably not running.
            stale = utcnow() - beat > timedelta(minutes=max(30, 3 * current.sync_interval_minutes))
        next_run = None
        runs = status.get("runs") or []
        if runs and not running and not current.sync_paused:
            finished = parse_iso(runs[0]["finished_at"])
            if finished:
                next_run = finished + timedelta(minutes=current.sync_interval_minutes)
        return {
            "current": current,
            "status": status,
            "running": running,
            "running_stale": stale,
            "next_run": next_run,
            "sync_pending": sync_requested(settings.state_dir),
        }

    @app.get("/settings", dependencies=authed)
    async def settings_page(request: Request) -> Response:
        overrides = load_overrides(settings.state_dir)
        fields = [
            {
                "name": name,
                "value": getattr(effective_settings(settings), name),
                "default": getattr(settings, name),
                "overridden": name in overrides,
            }
            for name in FORM_SETTINGS
        ]
        return render(request, "settings.html", fields=fields, cfg=settings, **status_context())

    @app.get("/settings/status", dependencies=authed)
    async def settings_status(request: Request) -> Response:
        # Not render(): polling must not consume flash messages.
        return templates.TemplateResponse(request, "_status.html", status_context())

    @app.post("/settings", dependencies=posted)
    async def save_settings(request: Request) -> Response:
        form = await request.form()
        overrides = load_overrides(settings.state_dir)
        if form.get("action") == "reset":
            overrides = {k: v for k, v in overrides.items() if k not in FORM_SETTINGS}
            save_overrides(settings.state_dir, overrides)
            flash(request, "Sync settings reset to the values from the environment.", "ok")
            return RedirectResponse("/settings", status_code=303)
        errors = []
        for name in FORM_SETTINGS:
            raw = "true" if name in CHECKBOXES and form.get(name) else form.get(name, "false")
            try:
                value = validate_value(name, raw)
            except ValueError as e:
                errors.append(f"{name.replace('_', ' ')}: {e}")
                continue
            # Only values that differ from the environment are stored, so later changes to the
            # environment still apply to everything not deliberately changed here.
            if value == getattr(settings, name):
                overrides.pop(name, None)
            else:
                overrides[name] = value
        if errors:
            for e in errors:
                flash(request, e, "error")
            return RedirectResponse("/settings", status_code=303)
        save_overrides(settings.state_dir, overrides)
        log.info("web_settings_saved", overrides=overrides)
        flash(request, "Settings saved. They apply from the next sync pass.", "ok")
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/pause", dependencies=posted)
    async def pause(request: Request) -> Response:
        form = await request.form()
        paused = form.get("paused") == "true"
        overrides = load_overrides(settings.state_dir)
        if paused == settings.sync_paused:
            overrides.pop("sync_paused", None)
        else:
            overrides["sync_paused"] = paused
        save_overrides(settings.state_dir, overrides)
        flash(request, "Scheduled sync paused." if paused else "Scheduled sync resumed.", "ok")
        return RedirectResponse("/settings", status_code=303)

    @app.post("/sync-now", dependencies=posted)
    async def sync_now(request: Request) -> Response:
        request_sync(settings.state_dir)
        flash(request, "Sync requested. The sync service starts a pass within a few seconds.", "ok")
        return RedirectResponse("/settings", status_code=303)

    assert set(FORM_SETTINGS) < set(EDITABLE)
    return app
