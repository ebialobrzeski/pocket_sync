# pocket-sync

A Docker container that periodically syncs recordings from [Pocket](https://heypocketai.com)
into a local archive on a NAS, plus a small web UI for browsing that archive:

- **audio** (`.ogg` / `.mp3`) goes to the audio directory,
- **metadata** (`raw.json`, transcript, summary, action items, `.meta.json`) and the state
  database go to the data directory,
- the **web UI** lets you listen to recordings, read transcripts and summaries, and manage the sync.

Both trees share the same relative layout `YYYY/MM/YYYY-MM-DD_HHMM_<title>_<id>/`.
The tool is idempotent and safe to restart. Audio is written through a `.part` file and verified
by size and SHA-256, so an interrupted transfer never leaves a corrupted file behind.

API details: [docs/api-notes.md](docs/api-notes.md). Project plan:
[pocket-nas-sync-plan.md](pocket-nas-sync-plan.md).

## Deploying straight from GitHub

The image is built locally on the NAS from the [Dockerfile](Dockerfile) in this repository
(`build: .` in [docker-compose.yml](docker-compose.yml)). No image registry is needed.
In Dockhand, add a stack from the Git repository `https://github.com/ebialobrzeski/pocket_sync.git`
(branch `main`, file `docker-compose.yml`).

The stack has two services built from the same image:

| service | what it does |
|---|---|
| `pocket-sync` | the sync loop |
| `pocket-sync-web` | the web UI on port `WEB_PORT` (default `8080`) |

### 1. Directories on the NAS

Create two directories (on the same or on different volumes) and find the UID/GID of their owner
(`id <user>` over SSH):

```
/volume1/docker/pocket-sync    # data: meta/ and state/ are created here (/data in the container)
/volume2/pocket-audio          # audio files (/audio in the container)
```

### 2. Environment variables

Create `.env` next to `docker-compose.yml` from [.env.example](.env.example)
(or set these variables in the Dockhand stack configuration):

```env
POCKET_API_KEY=pk_...
POCKET_DATA_PATH=/volume1/docker/pocket-sync
POCKET_AUDIO_PATH=/volume2/pocket-audio
PUID=1000
PGID=1000
TZ=Europe/Warsaw
WEB_PASSWORD=choose-a-long-password
```

`.env` should have `600` permissions and never be committed to the repository.

### 3. Start

```sh
docker compose up -d --build
docker compose logs -f
```

Then open `http://<nas>:8080` and log in with `WEB_PASSWORD`.

To update: `git pull` (or redeploy the stack in Dockhand), then run `docker compose up -d --build` again.

### Docker secret instead of a variable

Instead of `POCKET_API_KEY` you can pass `POCKET_API_KEY_FILE=/run/secrets/pocket_api_key`
(example commented out in `docker-compose.yml`). The same works for `WEB_PASSWORD_FILE`.

## Web UI

- **Recordings**: all recordings grouped by month, newest first. Search matches titles, or
  titles, transcripts and summaries when you tick the checkbox. Search ignores case and diacritics,
  so `lodz` finds `Łódź`.
- **Recording page**: audio player with ±15 s skip buttons and playback speed; rendered summary
  (every summarization, if there are several); action items; and a transcript with timestamps.
  Click a timestamp to jump to that moment. The current segment is highlighted and followed while
  playing. The page also has a "find in transcript" filter, links to the raw files
  (`summary.md`, `transcript.json`, `raw.json`, …) and a **Re-sync** button that makes the next
  pass fetch the recording again.
- **Sync**: live status (idle / syncing / paused / not responding, last successful pass, next pass),
  **Sync now**, **Pause / Resume schedule**, recordings with sync errors (with **Retry**), recent
  passes, and a settings form.

### Sync settings from the UI

These settings can be changed in the UI without restarting anything:
download audio, sync interval, parallel downloads and full refresh interval, plus pause.
They are stored in `STATE_DIR/settings.json` and take precedence over the environment. Only values
that differ from the environment are stored, and **Reset to environment** removes them.
The sync loop re-reads them before every pass. While it waits between passes it checks every few
seconds for "Sync now" requests (the `STATE_DIR/sync-now` file) and for a changed interval.

Paths, the API key and the time zone stay environment-only.

### Security

- Protected by a single master password (`WEB_PASSWORD`, at least 8 characters). The session is a
  signed cookie (HttpOnly, SameSite=Lax) valid for `WEB_SESSION_HOURS`. Changing the password
  logs out every session.
- After 10 failed logins within 15 minutes, a client is blocked for the rest of that window.
- Forms carry CSRF tokens, and responses send a strict Content-Security-Policy.
- The UI serves plain HTTP. To reach it from outside your LAN, put it behind a reverse proxy with
  HTTPS (e.g. the Synology reverse proxy) and set `WEB_COOKIE_SECURE=true`.
- The audio directory is mounted read-only in the web container.

## Commands

```sh
docker compose exec pocket-sync python -m pocket_sync verify              # archive consistency
docker compose exec pocket-sync python -m pocket_sync verify --checksums  # + SHA-256 of audio
docker compose exec pocket-sync python -m pocket_sync healthcheck
docker compose run --rm -e RUN_ONCE=true pocket-sync                      # a single pass
```

`verify` exits 0 for a healthy archive and 1 when it finds problems, so it can be hooked into
cron. It detects missing and mismatching audio files, divergence between the audio and metadata
trees, and orphaned `.part` files.

Exit codes: `0` OK, `1` problems (failed recordings / verify), `2` configuration error
(e.g. missing key or password), `3` storage unavailable.

## Configuration

| variable | default | description |
|---|---|---|
| `POCKET_API_KEY` | — | `pk_...` key, required for the sync (or `POCKET_API_KEY_FILE`) |
| `POCKET_API_BASE` | `https://public.heypocketai.com/api/v1` | API base URL |
| `META_DIR` | `/data/meta/pocket` | metadata tree |
| `STATE_DIR` | `/data/state` | state DB, runtime settings and logs |
| `AUDIO_DIR` | `/audio` | audio tree |
| `DOWNLOAD_AUDIO` | `true` | `false` skips audio downloads (editable in the UI) |
| `SYNC_INTERVAL_MINUTES` | `15` | time between passes (editable in the UI) |
| `RUN_ONCE` | `false` | run one pass and exit |
| `MAX_CONCURRENCY` | `3` | recordings processed in parallel (editable in the UI) |
| `FULL_REFRESH_HOURS` | `24` | how often to re-fetch details of every recording, 0 = off (editable in the UI) |
| `SYNC_PAUSED` | `false` | skip scheduled passes (usually toggled in the UI) |
| `TZ` | `UTC` | time zone for directory names and the UI |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `LOG_FORMAT` | `json` | `json` or `console` |
| `LOG_TO_FILE` | `false` | also log to `STATE_DIR/logs/pocket-sync.log` (rotated, 5×10 MB) |
| `WEB_PASSWORD` | — | web UI master password, required for the UI (or `WEB_PASSWORD_FILE`) |
| `WEB_PORT` | `8080` | web UI port (inside the container and on the host) |
| `WEB_HOST` | `0.0.0.0` | web UI bind address |
| `WEB_SESSION_HOURS` | `168` | how long a login lasts |
| `WEB_COOKIE_SECURE` | `false` | `true` when the UI is served over HTTPS |
| `WEB_SECRET_KEY` | derived from the password | key used to sign session cookies |

## Archive layout

```
data   /data/meta/pocket/2026/09/2026-09-25_1140_aktualizacja-sql-server-2016-do-2019_desktop_1790329209135_lpmlvi/
           raw.json          full API response (source of truth)
           transcript.json   segments with timestamps (and speakers, when available)
           transcript.md
           summary.md        all AI summaries
           actions.json      action items
           .meta.json        audio path relative to the audio dir, SHA-256, size, file hashes, tool version
       /data/state/pocket-sync.db
       /data/state/settings.json   sync settings changed in the web UI (only when changed)

audio  /audio/2026/09/2026-09-25_1140_aktualizacja-sql-server-2016-do-2019_desktop_1790329209135_lpmlvi/
           audio.ogg
```

## How a pass works

1. Checks that `META_DIR`, `STATE_DIR` and `AUDIO_DIR` are writable (the latter only with
   `DOWNLOAD_AUDIO=true`) and removes `.part` files older than 24 h.
2. Fetches the full list of recordings (pages of 100).
3. Queues recordings that are new, whose list entry changed (`updated_at`, title, folder, tags),
   whose processing on the Pocket side is incomplete, whose audio is not `done`/`skipped`, and
   those whose details were not fetched for `FULL_REFRESH_HOURS`. Recordings still being
   processed (`state != completed`) wait.
4. For each one: details → `raw.json` → audio (fresh presigned URL, `.part`, SHA-256,
   `Content-Length` check, `os.replace` + `fsync`) → derived files → `.meta.json` → DB row.
   Files whose content did not change are not rewritten.
5. A failure of one recording does not stop the pass. After 5 consecutive failures a recording
   is logged as `recording_needs_attention` and retried less and less often (at most once every
   24 h). It is also listed on the web UI's Sync page, where **Retry** clears the backoff.

The client respects the API rate limit (50 requests/min, `X-Ratelimit-*` headers), retries 429/5xx
with exponential backoff and `Retry-After`, and does not retry other 4xx errors.

## Local development

```sh
python -m venv .venv && .venv/bin/pip install -e ".[dev]"   # Windows: .venv\Scripts\pip
.venv/bin/pytest

# a pass on a laptop without audio, with the key from .env:
META_DIR=./data/meta STATE_DIR=./data/state DOWNLOAD_AUDIO=false LOG_FORMAT=console \
  .venv/bin/python -m pocket_sync once

# the web UI on http://localhost:8080 for the same data:
META_DIR=./data/meta STATE_DIR=./data/state AUDIO_DIR=./data/audio WEB_PASSWORD=dev-password \
  LOG_FORMAT=console .venv/bin/python -m pocket_sync web

docker build -t pocket-sync .
```
