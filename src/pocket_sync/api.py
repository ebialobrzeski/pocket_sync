"""Pocket API client: auth, pagination, rate limiting, retries and audio streaming."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
import structlog
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from .models import AudioUrl, Folder, RecordingSummary
from .writer import PartFile

log = structlog.get_logger(__name__)

API_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
DOWNLOAD_TIMEOUT = httpx.Timeout(300.0, connect=10.0)
MAX_ATTEMPTS = 5
MAX_RETRY_AFTER = 300.0
PAGE_SIZE = 100
CHUNK_SIZE = 1024 * 1024

Sleep = Callable[[float], Awaitable[None]]


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class PermanentApiError(ApiError):
    """4xx other than 429: retrying in this run will not help."""


class NotFoundError(PermanentApiError):
    pass


class AudioNotAvailable(NotFoundError):
    """The recording exists but has no audio file (e.g. the onboarding recording)."""


class TransientApiError(ApiError):
    """429 / 5xx: worth retrying, optionally after `retry_after` seconds."""

    def __init__(self, message: str, status: int | None = None, retry_after: float | None = None):
        super().__init__(message, status)
        self.retry_after = retry_after


class AudioUrlExpired(TransientApiError):
    """S3 rejected the presigned URL (403/400): fetch a fresh URL and try again."""


RETRYABLE = (TransientApiError, httpx.TransportError)


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError):
        return None


def _error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("error"):
            return str(body["error"])
    except ValueError:
        pass
    return resp.text[:200] or resp.reason_phrase


class PocketClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        sleep: Sleep = asyncio.sleep,
        max_attempts: int = MAX_ATTEMPTS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._api = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=API_TIMEOUT,
        )
        # Separate client without the Authorization header: presigned S3 URLs must not get it.
        self._download = httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT, follow_redirects=True)
        self._sleep = sleep
        self._clock = clock
        self._max_attempts = max_attempts
        self._rl_lock = asyncio.Lock()
        self._rl_remaining: int | None = None
        self._rl_reset: float | None = None
        self.requests_made = 0

    async def __aenter__(self) -> PocketClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._api.aclose()
        await self._download.aclose()

    # --- retry plumbing -------------------------------------------------------------------

    def _wait(self, state: RetryCallState) -> float:
        exc = state.outcome.exception() if state.outcome else None
        if isinstance(exc, TransientApiError) and exc.retry_after is not None:
            return min(exc.retry_after, MAX_RETRY_AFTER)
        return wait_exponential_jitter(initial=1, max=60)(state)

    def _before_sleep(self, state: RetryCallState) -> None:
        exc = state.outcome.exception() if state.outcome else None
        log.warning(
            "api_retry",
            attempt=state.attempt_number,
            wait_s=round(state.next_action.sleep, 2) if state.next_action else None,
            error=str(exc),
        )

    def retrying(self, attempts: int | None = None) -> AsyncRetrying:
        return AsyncRetrying(
            stop=stop_after_attempt(attempts or self._max_attempts),
            retry=retry_if_exception_type(RETRYABLE),
            wait=self._wait,
            sleep=self._sleep,
            before_sleep=self._before_sleep,
            reraise=True,
        )

    # --- rate limiting --------------------------------------------------------------------

    async def _throttle(self) -> None:
        async with self._rl_lock:
            if self._rl_remaining is not None and self._rl_remaining <= 0 and self._rl_reset:
                delay = self._rl_reset - self._clock() + 0.5
                if delay > 0:
                    log.info("rate_limit_wait", wait_s=round(delay, 1))
                    await self._sleep(min(delay, MAX_RETRY_AFTER))
                self._rl_remaining = None

    def _track_rate_limit(self, resp: httpx.Response) -> None:
        try:
            if "x-ratelimit-remaining" in resp.headers:
                self._rl_remaining = int(resp.headers["x-ratelimit-remaining"])
            if "x-ratelimit-reset" in resp.headers:
                self._rl_reset = float(resp.headers["x-ratelimit-reset"])
        except ValueError:
            pass

    # --- requests -------------------------------------------------------------------------

    async def _request_once(self, method: str, path: str, params: dict[str, Any] | None) -> Any:
        await self._throttle()
        self.requests_made += 1
        resp = await self._api.request(method, path, params=params)
        self._track_rate_limit(resp)
        status = resp.status_code
        if status == 429:
            retry_after = _parse_retry_after(resp.headers.get("retry-after"))
            if retry_after is None and self._rl_reset:
                retry_after = max(0.0, self._rl_reset - self._clock() + 0.5)
            raise TransientApiError(f"rate limited: {_error_message(resp)}", status, retry_after)
        if status >= 500:
            raise TransientApiError(f"server error {status}: {_error_message(resp)}", status)
        if status == 404:
            msg = _error_message(resp)
            cls = AudioNotAvailable if "audio" in msg.lower() else NotFoundError
            raise cls(msg, status)
        if status >= 400:
            raise PermanentApiError(f"HTTP {status}: {_error_message(resp)}", status)
        try:
            body = resp.json()
        except ValueError as e:
            raise TransientApiError(f"invalid JSON from {path}", status) from e
        if isinstance(body, dict) and body.get("success") is False:
            raise PermanentApiError(f"API error: {body.get('error')}", status)
        return body

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        body: Any = None
        async for attempt in self.retrying():
            with attempt:
                body = await self._request_once("GET", path, params)
        return body

    async def list_recordings(self, page_size: int = PAGE_SIZE) -> list[RecordingSummary]:
        items: list[RecordingSummary] = []
        seen: set[str] = set()
        page = 1
        while True:
            body = await self._get("/public/recordings", {"page": page, "limit": page_size})
            for raw in body.get("data") or []:
                rec = RecordingSummary.model_validate(raw)
                if rec.id not in seen:  # page-based pagination can shift under concurrent inserts
                    seen.add(rec.id)
                    items.append(rec)
            pagination = body.get("pagination") or {}
            total_pages = pagination.get("total_pages") or 0
            if not pagination.get("has_more") or not body.get("data"):
                break
            if total_pages and page >= total_pages:
                break
            page += 1
        return items

    async def get_recording(self, recording_id: str) -> dict[str, Any]:
        body = await self._get(f"/public/recordings/{recording_id}")
        data = body.get("data")
        if not isinstance(data, dict):
            raise PermanentApiError(f"recording {recording_id}: response has no data object")
        return data

    async def get_audio_url(self, recording_id: str, expires_in: int = 900) -> AudioUrl:
        body = await self._get(
            f"/public/recordings/{recording_id}/audio-url", {"expires_in": expires_in}
        )
        return AudioUrl.model_validate(body.get("data") or {})

    async def list_folders(self) -> list[Folder]:
        body = await self._get("/public/folders")
        return [Folder.model_validate(f) for f in body.get("data") or []]

    async def download(self, url: str, part: PartFile) -> str | None:
        """Stream `url` into `part` and commit it. Returns the Content-Type.

        The caller owns `part`: on any exception it must call `part.abort()`.
        """
        async with self._download.stream("GET", url) as resp:
            status = resp.status_code
            if status in (400, 403):
                raise AudioUrlExpired(f"audio URL rejected with HTTP {status}", status)
            if status == 429 or status >= 500:
                retry_after = _parse_retry_after(resp.headers.get("retry-after"))
                raise TransientApiError(f"audio download HTTP {status}", status, retry_after)
            if status >= 400:
                raise PermanentApiError(f"audio download HTTP {status}", status)
            expected: int | None = None
            if "content-length" in resp.headers and not resp.headers.get("content-encoding"):
                expected = int(resp.headers["content-length"])
            part.open()
            async for chunk in resp.aiter_bytes(CHUNK_SIZE):
                part.write(chunk)
            part.commit(expected_size=expected)
            return resp.headers.get("content-type")
