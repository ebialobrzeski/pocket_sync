from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from pocket_sync.api import PocketClient
from pocket_sync.config import Settings
from pocket_sync.state import StateDB

BASE = "https://pocket.test/api/v1"
S3 = "https://s3.test"


@dataclass
class FakeRecording:
    item: dict[str, Any]
    details: dict[str, Any]
    audio: bytes | None = b"OggS" + b"\x00" * 2048
    ext: str = ".ogg"


def list_item(
    rid: str,
    title: str = "Plany przyjazdu",
    recording_at: str = "2026-09-25T08:09:00Z",
    updated_at: str = "2026-09-25T08:20:00Z",
    state: str = "completed",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "id": rid,
        "title": title,
        "folder_id": None,
        "duration": 109,
        "state": state,
        "language": None,
        "recording_at": recording_at,
        "created_at": recording_at,
        "updated_at": updated_at,
        "tags": [],
        **extra,
    }


def details_for(item: dict[str, Any], summary: str = "### Teza\nSpotkanie o przyjeździe.") -> dict[str, Any]:
    return {
        **copy.deepcopy(item),
        "transcript": {
            "metadata": {"duration": 105.5, "source": "smallest"},
            "segments": [
                {"start": 0.5, "end": 3.0, "text": "Dzień dobry.", "originalText": "Dzień dobry.", "speaker": "A"},
                {"start": 3.2, "end": 6.0, "text": "Kiedy przyjeżdżacie?", "originalText": "x", "speaker": "B"},
            ],
            "text": "Dzień dobry. Kiedy przyjeżdżacie?",
        },
        "summarizations": {
            "sum-1": {
                "id": "s1",
                "summarizationId": "sum-1",
                "processingStatus": "completed",
                "v2": {
                    "summary": {"markdown": summary, "version": "1"},
                    "actionItems": {
                        "version": "3",
                        "actions": [{"id": "book", "label": "Zarezerwować hotel", "status": "TODO"}],
                    },
                },
                "createdAt": "2026-09-25T08:15:00Z",
            }
        },
    }


@dataclass
class FakePocket:
    """In-memory Pocket API + S3 served through respx."""

    router: respx.MockRouter
    recordings: dict[str, FakeRecording] = field(default_factory=dict)
    calls: dict[str, int] = field(default_factory=dict)
    s3_failures: dict[str, list[httpx.Response]] = field(default_factory=dict)
    details_failures: dict[str, list[httpx.Response]] = field(default_factory=dict)
    url_counter: int = 0

    def add(self, rid: str, **kwargs: Any) -> FakeRecording:
        item = list_item(rid, **kwargs)
        rec = FakeRecording(item=item, details=details_for(item))
        self.recordings[rid] = rec
        return rec

    def _count(self, key: str) -> None:
        self.calls[key] = self.calls.get(key, 0) + 1

    def count(self, prefix: str) -> int:
        return sum(v for k, v in self.calls.items() if k.startswith(prefix))

    def reset_calls(self) -> None:
        self.calls.clear()

    def install(self) -> None:
        r = self.router
        r.get(f"{BASE}/public/recordings").mock(side_effect=self._list)
        r.get(url__regex=rf"^{re.escape(BASE)}/public/recordings/(?P<rid>[^/?]+)/audio-url").mock(
            side_effect=self._audio_url
        )
        r.get(url__regex=rf"^{re.escape(BASE)}/public/recordings/(?P<rid>[^/?]+)(\?.*)?$").mock(
            side_effect=self._details
        )
        r.get(f"{BASE}/public/folders").mock(
            return_value=httpx.Response(200, json={"success": True, "data": []})
        )
        r.get(url__regex=rf"^{re.escape(S3)}/(?P<rid>[^/?]+)\.\w+").mock(side_effect=self._s3)

    def _list(self, request: httpx.Request) -> httpx.Response:
        self._count("list")
        page = int(request.url.params.get("page", 1))
        limit = int(request.url.params.get("limit", 20))
        items = [r.item for r in self.recordings.values()]
        chunk = items[(page - 1) * limit : page * limit]
        total_pages = max(1, -(-len(items) // limit))
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": chunk,
                "pagination": {
                    "page": page,
                    "limit": limit,
                    "total": len(items),
                    "total_pages": total_pages,
                    "has_more": page < total_pages,
                },
            },
        )

    def _details(self, request: httpx.Request, rid: str) -> httpx.Response:
        self._count(f"details:{rid}")
        if self.details_failures.get(rid):
            return self.details_failures[rid].pop(0)
        rec = self.recordings.get(rid)
        if not rec:
            return httpx.Response(404, json={"success": False, "error": "recording not found"})
        return httpx.Response(200, json={"success": True, "data": rec.details})

    def _audio_url(self, request: httpx.Request, rid: str) -> httpx.Response:
        self._count(f"audio-url:{rid}")
        rec = self.recordings.get(rid)
        if not rec or rec.audio is None:
            return httpx.Response(404, json={"success": False, "error": "recording audio file not found"})
        self.url_counter += 1
        url = f"{S3}/{rid}{rec.ext}?X-Amz-Signature=sig{self.url_counter}"
        return httpx.Response(
            200, json={"success": True, "data": {"signed_url": url, "expires_in": 900, "expires_at": "2026-09-25T12:00:00Z"}}
        )

    def _s3(self, request: httpx.Request, rid: str) -> httpx.Response:
        self._count(f"s3:{rid}")
        if self.s3_failures.get(rid):
            return self.s3_failures[rid].pop(0)
        rec = self.recordings[rid]
        assert rec.audio is not None
        assert "authorization" not in request.headers, "API key must not be sent to S3"
        return httpx.Response(200, content=rec.audio, headers={"Content-Type": "audio/ogg"})


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def no_sleep(_: float) -> None:
    return None


@pytest.fixture
def dirs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    data = tmp_path_factory.mktemp("data")
    audio = tmp_path_factory.mktemp("audio")
    return {"meta": data / "meta" / "pocket", "state": data / "state", "audio": audio / "pocket-audio"}


@pytest.fixture
def make_settings(dirs: dict[str, Path]):
    def factory(**overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "pocket_api_key": "pk_test",
            "pocket_api_base": BASE,
            "meta_dir": dirs["meta"],
            "state_dir": dirs["state"],
            "audio_dir": dirs["audio"],
            "tz": "Europe/Warsaw",
            "full_refresh_hours": 0,
            "log_format": "console",
        }
        values.update(overrides)
        return Settings(_env_file=None, **values)

    return factory


@pytest.fixture
def settings(make_settings) -> Settings:
    return make_settings()


@pytest.fixture
def fake():
    with respx.mock(assert_all_called=False) as router:
        fp = FakePocket(router)
        fp.install()
        yield fp


@pytest.fixture
async def client():
    async with PocketClient(BASE, "pk_test", sleep=no_sleep) as c:
        yield c


@pytest.fixture
def db(settings: Settings):
    with StateDB(settings.db_path) as d:
        d.migrate()
        yield d
