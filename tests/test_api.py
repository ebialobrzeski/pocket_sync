from __future__ import annotations

import httpx
import pytest
import respx

from pocket_sync.api import (
    AudioNotAvailable,
    AudioUrlExpired,
    NotFoundError,
    PermanentApiError,
    PocketClient,
    TransientApiError,
)
from pocket_sync.writer import PartFile

from .conftest import BASE, FakePocket, no_sleep


async def test_pagination_collects_all_pages(fake: FakePocket, client: PocketClient):
    for i in range(5):
        fake.add(f"r{i}")
    recs = await client.list_recordings(page_size=2)
    assert [r.id for r in recs] == ["r0", "r1", "r2", "r3", "r4"]
    assert fake.count("list") == 3


async def test_unknown_fields_are_accepted(fake: FakePocket, client: PocketClient):
    fake.add("r1", brand_new_field={"x": 1}, tags=[{"id": "t", "name": "Praca", "color": "#fff", "extra": 1}])
    (rec,) = await client.list_recordings()
    assert rec.model_extra == {"brand_new_field": {"x": 1}}
    assert rec.tags[0].name == "Praca"


async def test_429_respects_retry_after():
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)

    with respx.mock() as router:
        route = router.get(f"{BASE}/public/folders").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "7"}, json={"success": False, "error": "slow down"}),
                httpx.Response(200, json={"success": True, "data": []}),
            ]
        )
        async with PocketClient(BASE, "pk_test", sleep=sleep) as client:
            assert await client.list_folders() == []
    assert route.call_count == 2
    assert slept == [7.0]


async def test_500_is_retried_then_succeeds(client: PocketClient):
    with respx.mock() as router:
        route = router.get(f"{BASE}/public/folders").mock(
            side_effect=[
                httpx.Response(500, json={"success": False, "error": "boom"}),
                httpx.Response(503),
                httpx.Response(200, json={"success": True, "data": [{"id": "f", "name": "Work", "children": []}]}),
            ]
        )
        folders = await client.list_folders()
    assert route.call_count == 3
    assert folders[0].name == "Work"


async def test_500_gives_up_after_five_attempts(client: PocketClient):
    with respx.mock() as router:
        route = router.get(f"{BASE}/public/folders").mock(return_value=httpx.Response(500))
        with pytest.raises(TransientApiError):
            await client.list_folders()
    assert route.call_count == 5


async def test_4xx_is_not_retried(client: PocketClient):
    with respx.mock() as router:
        route = router.get(f"{BASE}/public/folders").mock(
            return_value=httpx.Response(401, json={"success": False, "error": "API key not found"})
        )
        with pytest.raises(PermanentApiError) as exc:
            await client.list_folders()
    assert route.call_count == 1
    assert exc.value.status == 401


async def test_404_audio_is_distinguished(fake: FakePocket, client: PocketClient):
    rec = fake.add("r1")
    rec.audio = None
    with pytest.raises(AudioNotAvailable):
        await client.get_audio_url("r1")
    with pytest.raises(NotFoundError):
        await client.get_recording("missing")


async def test_transport_errors_are_retried(client: PocketClient):
    with respx.mock() as router:
        route = router.get(f"{BASE}/public/folders").mock(
            side_effect=[httpx.ConnectTimeout("t"), httpx.Response(200, json={"success": True, "data": []})]
        )
        await client.list_folders()
    assert route.call_count == 2


async def test_rate_limit_headers_throttle_next_request():
    slept: list[float] = []
    now = [1000.0]

    async def sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    ok = {"success": True, "data": []}
    with respx.mock() as router:
        router.get(f"{BASE}/public/folders").mock(
            side_effect=[
                httpx.Response(200, json=ok, headers={"X-Ratelimit-Remaining": "0", "X-Ratelimit-Reset": "1030"}),
                httpx.Response(200, json=ok, headers={"X-Ratelimit-Remaining": "49", "X-Ratelimit-Reset": "1090"}),
            ]
        )
        async with PocketClient(BASE, "pk_test", sleep=sleep, clock=lambda: now[0]) as client:
            await client.list_folders()
            assert slept == []
            await client.list_folders()
    assert slept == [pytest.approx(30.5)]


async def test_expired_audio_url_raises_specific_error(tmp_path, client: PocketClient):
    with respx.mock() as router:
        router.get("https://s3.test/a.ogg").mock(return_value=httpx.Response(403, text="Request has expired"))
        part = PartFile(tmp_path / "audio.ogg")
        with pytest.raises(AudioUrlExpired):
            await client.download("https://s3.test/a.ogg", part)
    assert not (tmp_path / "audio.ogg").exists()


async def test_download_streams_and_hashes(tmp_path):
    data = b"x" * (3 * 1024 * 1024 + 17)
    with respx.mock() as router:
        router.get("https://s3.test/a.ogg").mock(
            return_value=httpx.Response(200, content=data, headers={"Content-Type": "audio/ogg"})
        )
        async with PocketClient(BASE, "pk_test", sleep=no_sleep) as client:
            part = PartFile(tmp_path / "audio.ogg")
            ctype = await client.download("https://s3.test/a.ogg", part)
    assert ctype == "audio/ogg"
    assert (tmp_path / "audio.ogg").read_bytes() == data
    assert part.size == len(data)
    assert not part.part_path.exists()
