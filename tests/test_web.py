from __future__ import annotations

import asyncio
import json
import re

import httpx
import pytest
import respx

from pocket_sync.__main__ import wait_for_next_run
from pocket_sync.api import PocketClient
from pocket_sync.config import ConfigError
from pocket_sync.runtime import (
    effective_settings,
    load_overrides,
    request_sync,
    save_overrides,
    sync_requested,
    take_sync_request,
)
from pocket_sync.state import StateDB
from pocket_sync.sync import Syncer
from pocket_sync.web import create_app
from pocket_sync.web.archive import fold, render_markdown, snippet

from .conftest import BASE, FakePocket, details_for, no_sleep

PASSWORD = "correct horse battery"


@pytest.fixture
async def archived(make_settings):
    """Settings pointing at an archive filled by a real sync pass against the fake API."""
    settings = make_settings(web_password=PASSWORD)
    with respx.mock(assert_all_called=False) as router:
        fake = FakePocket(router)
        fake.install()
        fake.add("r1")
        r2 = fake.add("r2", title="Wycieczka do Łodzi", recording_at="2026-08-02T15:30:00Z")
        r2.details = details_for(r2.item, summary="### Plan\n<script>alert(1)</script>\n- bilety")
        r2.audio = None  # no audio in Pocket -> audio_status=skipped
        with StateDB(settings.db_path) as db:
            db.migrate()
            async with PocketClient(BASE, "pk_test", sleep=no_sleep) as api:
                await Syncer(settings, api, db).run_once()
    return settings


@pytest.fixture
async def client(archived):
    app = create_app(archived)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


def csrf_of(html: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, "no csrf token in page"
    return m.group(1)


async def login(client: httpx.AsyncClient, password: str = PASSWORD) -> httpx.Response:
    page = await client.get("/login")
    return await client.post("/login", data={"csrf": csrf_of(page.text), "password": password, "next": "/"})


@pytest.fixture
async def authed(client):
    r = await login(client)
    assert r.status_code == 303
    return client


# --- auth ---------------------------------------------------------------------------------


async def test_requires_login(client):
    for path in ("/", "/settings", "/recordings/r1", "/recordings/r1/audio", "/recordings/r1/files/raw.json"):
        r = await client.get(path)
        assert r.status_code == 303, path
        assert r.headers["location"].startswith("/login?next=")
    assert (await client.get("/healthz")).text == "ok"


async def test_login_wrong_password_and_throttle(client):
    r = await login(client, "nope")
    assert r.status_code == 401 and "Wrong password" in r.text
    for _ in range(10):
        await login(client, "nope")
    r = await login(client)  # even the right password is refused while throttled
    assert r.status_code == 429


async def test_login_redirects_to_local_next_only(client):
    page = await client.get("/login")
    r = await client.post(
        "/login", data={"csrf": csrf_of(page.text), "password": PASSWORD, "next": "//evil.example/x"}
    )
    assert r.headers["location"] == "/"


async def test_logout(authed):
    page = await authed.get("/")
    r = await authed.post("/logout", data={"csrf": csrf_of(page.text)})
    assert r.status_code == 303
    assert (await authed.get("/")).status_code == 303


async def test_post_without_csrf_is_rejected(authed, archived):
    r = await authed.post("/sync-now", data={})
    assert r.status_code == 400
    assert not sync_requested(archived.state_dir)


def test_app_needs_password(make_settings):
    with pytest.raises(ConfigError, match="WEB_PASSWORD"):
        create_app(make_settings())
    with pytest.raises(ConfigError, match="too short"):
        create_app(make_settings(web_password="short"))


# --- recordings ---------------------------------------------------------------------------


async def test_list_and_search(authed):
    r = await authed.get("/")
    assert r.status_code == 200
    assert "Plany przyjazdu" in r.text and "Wycieczka do Łodzi" in r.text
    assert "September 2026" in r.text and "August 2026" in r.text
    assert r.headers["content-security-policy"].startswith("default-src 'self'")

    r = await authed.get("/", params={"q": "lodzi"})  # diacritics-insensitive
    assert "Wycieczka do Łodzi" in r.text and "Plany przyjazdu" not in r.text

    r = await authed.get("/", params={"q": "przyjezdzacie"})  # only in the transcript
    assert "Nothing found" in r.text
    r = await authed.get("/", params={"q": "przyjezdzacie", "content": "true"})
    assert "Plany przyjazdu" in r.text and "Kiedy przyjeżdżacie?" in r.text  # snippet


async def test_recording_page(authed):
    r = await authed.get("/recordings/r1")
    assert r.status_code == 200
    assert "<h3>Teza</h3>" in r.text  # summary rendered from markdown
    assert 'data-start="3.2"' in r.text and "Kiedy przyjeżdżacie?" in r.text
    assert "Zarezerwować hotel" in r.text
    assert 'src="/recordings/r1/audio"' in r.text

    r = await authed.get("/recordings/r2")
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;" in r.text
    assert "no audio file in Pocket" in r.text

    assert (await authed.get("/recordings/nope")).status_code == 404


async def test_audio_supports_range(authed, archived):
    full = await authed.get("/recordings/r1/audio")
    assert full.status_code == 200 and full.headers["content-type"] == "audio/ogg"
    assert full.content.startswith(b"OggS")
    part = await authed.get("/recordings/r1/audio", headers={"Range": "bytes=0-3"})
    assert part.status_code == 206 and part.content == b"OggS"
    assert (await authed.get("/recordings/r2/audio")).status_code == 404


async def test_meta_files(authed):
    r = await authed.get("/recordings/r1/files/transcript.md")
    assert r.status_code == 200 and "Kiedy przyjeżdżacie?" in r.text
    assert (await authed.get("/recordings/r1/files/pocket-sync.db")).status_code == 404
    assert (await authed.get("/recordings/r1/files/..%2F..%2Fstate")).status_code == 404


async def test_resync_clears_errors_and_requests_sync(authed, archived):
    with StateDB(archived.db_path) as db:
        db.upsert_recording("r1", rel_dir=db.get_recording("r1")["rel_dir"], error_count=6, last_error="boom")
    page = await authed.get("/settings")
    assert "Recordings needing attention" in page.text and "boom" in page.text
    r = await authed.post("/recordings/r1/resync", data={"csrf": csrf_of(page.text), "next": "/settings"})
    assert r.headers["location"] == "/settings"
    with StateDB(archived.db_path) as db:
        row = db.get_recording("r1")
    assert (row["error_count"], row["last_error"], row["list_hash"]) == (0, None, None)
    assert sync_requested(archived.state_dir)


# --- settings -----------------------------------------------------------------------------


async def test_settings_save_and_reset(authed, archived):
    page = await authed.get("/settings")
    assert page.status_code == 200 and "Recent passes" in page.text
    token = csrf_of(page.text)
    form = {"csrf": token, "action": "save", "sync_interval_minutes": "30", "max_concurrency": "3",
            "full_refresh_hours": "0"}  # download_audio unchecked
    r = await authed.post("/settings", data=form)
    assert r.status_code == 303
    # max_concurrency and full_refresh_hours equal the environment and are not stored
    assert load_overrides(archived.state_dir) == {"sync_interval_minutes": 30.0, "download_audio": False}
    current = effective_settings(archived)
    assert current.sync_interval_minutes == 30 and current.download_audio is False

    r = await authed.post("/settings", data={**form, "max_concurrency": "99"})
    assert "less than or equal to 20" in (await authed.get("/settings")).text
    assert load_overrides(archived.state_dir)["sync_interval_minutes"] == 30.0

    await authed.post("/settings", data={"csrf": token, "action": "reset"})
    assert load_overrides(archived.state_dir) == {}


async def test_pause_and_sync_now(authed, archived):
    page = await authed.get("/settings")
    token = csrf_of(page.text)
    await authed.post("/settings/pause", data={"csrf": token, "paused": "true"})
    assert effective_settings(archived).sync_paused is True
    assert "Resume schedule" in (await authed.get("/settings")).text
    status = await authed.get("/settings/status")
    assert "Paused" in status.text
    await authed.post("/settings/pause", data={"csrf": token, "paused": "false"})
    assert load_overrides(archived.state_dir) == {}

    await authed.post("/sync-now", data={"csrf": token})
    assert sync_requested(archived.state_dir)


# --- runtime & helpers --------------------------------------------------------------------


def test_overrides_ignore_invalid_entries(settings):
    (settings.state_dir).mkdir(parents=True, exist_ok=True)
    (settings.state_dir / "settings.json").write_text(
        json.dumps({"max_concurrency": 0, "pocket_api_key": "pk_x", "sync_interval_minutes": 5}), encoding="utf-8"
    )
    assert load_overrides(settings.state_dir) == {"sync_interval_minutes": 5.0}
    with pytest.raises(ValueError):
        save_overrides(settings.state_dir, {"meta_dir": "/tmp"})


def test_sync_request_roundtrip(settings):
    assert not take_sync_request(settings.state_dir)
    request_sync(settings.state_dir)
    assert take_sync_request(settings.state_dir)
    assert not sync_requested(settings.state_dir)


async def test_wait_for_next_run_wakes_on_request(settings):
    save_overrides(settings.state_dir, {"sync_interval_minutes": 60})
    task = asyncio.create_task(wait_for_next_run(settings, poll_seconds=0.01))
    await asyncio.sleep(0.05)
    assert not task.done()
    request_sync(settings.state_dir)
    await asyncio.wait_for(task, 1)


async def test_wait_for_next_run_picks_up_shorter_interval(settings):
    save_overrides(settings.state_dir, {"sync_interval_minutes": 60})
    task = asyncio.create_task(wait_for_next_run(settings, poll_seconds=0.01))
    await asyncio.sleep(0.05)
    save_overrides(settings.state_dir, {"sync_interval_minutes": 0.0001})
    await asyncio.wait_for(task, 1)


def test_helpers():
    assert fold("Łódź ŻÓŁW") == "lodz zolw"
    assert snippet("a" * 200 + " Łódź " + "b" * 200, ["lodz"], radius=10).startswith("…aaaa")
    assert "<em>x</em>" in render_markdown("*x*")
    assert 'href="javascript:' not in render_markdown("[x](javascript:alert(1))")


def test_healthcheck_healthy_while_paused(settings, db):
    from pocket_sync.__main__ import healthcheck

    assert healthcheck(settings) == 1  # no successful run yet
    save_overrides(settings.state_dir, {"sync_paused": True})
    assert healthcheck(settings) == 0
