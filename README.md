# pocket-sync

Kontener Dockera, który okresowo synchronizuje nagrania z [Pocket](https://heypocketai.com)
do lokalnego archiwum na NAS-ie:

- **audio** (`.ogg` / `.mp3`) trafia do katalogu audio,
- **metadane** (`raw.json`, transkrypcja, podsumowanie, action items, `.meta.json`) oraz baza stanu
  trafiają do katalogu danych.

Oba drzewa mają identyczną strukturę względną `RRRR/MM/RRRR-MM-DD_HHMM_<tytuł>_<id>/`.
Narzędzie jest idempotentne i odporne na restart. Audio jest zapisywane przez plik `.part`
z weryfikacją rozmiaru i SHA-256, więc przerwany transfer nigdy nie zostawia uszkodzonego pliku.

Szczegóły API: [docs/api-notes.md](docs/api-notes.md). Plan projektu:
[pocket-nas-sync-plan.md](pocket-nas-sync-plan.md).

## Deployment prosto z GitHuba

Obraz jest budowany lokalnie na NAS-ie z [Dockerfile](Dockerfile) w repozytorium
(`build: .` w [docker-compose.yml](docker-compose.yml)). Nie jest potrzebny żaden rejestr obrazów.
W Dockhand wystarczy dodać stack z repozytorium Git `https://github.com/ebialobrzeski/pocket_sync.git`
(gałąź `main`, plik `docker-compose.yml`).

### 1. Katalogi na NAS-ie

Utwórz dwa katalogi (mogą leżeć na tym samym lub na różnych wolumenach) i ustal UID/GID ich właściciela (`id <użytkownik>` przez SSH):

```
/volume1/docker/pocket-sync    # dane: tu powstaną meta/ i state/ (w kontenerze /data)
/volume2/pocket-audio          # audio: pliki audio (w kontenerze /audio)
```

### 2. Zmienne środowiskowe

Utwórz `.env` obok `docker-compose.yml` na podstawie [.env.example](.env.example)
(albo ustaw te zmienne w konfiguracji stacka w Dockhand):

```env
POCKET_API_KEY=pk_...
POCKET_DATA_PATH=/volume1/docker/pocket-sync
POCKET_AUDIO_PATH=/volume2/pocket-audio
PUID=1000
PGID=1000
TZ=Europe/Warsaw
```

Plik `.env` powinien mieć uprawnienia `600` i nigdy nie trafia do repozytorium.

### 3. Uruchomienie

```sh
docker compose up -d --build
docker compose logs -f
```

Aktualizacja: `git pull` (lub redeploy stacka w Dockhand) i ponownie `docker compose up -d --build`.

### Docker secret zamiast zmiennej

Zamiast `POCKET_API_KEY` można przekazać `POCKET_API_KEY_FILE=/run/secrets/pocket_api_key`
(przykład zakomentowany w `docker-compose.yml`).

## Polecenia

```sh
docker compose exec pocket-sync python -m pocket_sync verify              # spójność archiwum
docker compose exec pocket-sync python -m pocket_sync verify --checksums  # + SHA-256 audio
docker compose exec pocket-sync python -m pocket_sync healthcheck
docker compose run --rm -e RUN_ONCE=true pocket-sync                      # jeden przebieg
```

`verify` zwraca kod 0 dla zdrowego archiwum i 1 po wykryciu problemów, więc można go podpiąć
pod cron. Wykrywa brakujące i niezgodne pliki audio, rozjazd drzew audio i metadanych oraz osierocone `.part`.

Kody wyjścia: `0` OK, `1` problemy (nieudane nagrania / verify), `2` błąd konfiguracji
(np. brak klucza), `3` niedostępny storage.

## Konfiguracja

| zmienna | domyślna | opis |
|---|---|---|
| `POCKET_API_KEY` | — | klucz `pk_...`, wymagany (albo `POCKET_API_KEY_FILE`) |
| `POCKET_API_BASE` | `https://public.heypocketai.com/api/v1` | bazowy adres API |
| `META_DIR` | `/data/meta/pocket` | drzewo metadanych |
| `STATE_DIR` | `/data/state` | baza stanu i logi |
| `AUDIO_DIR` | `/audio` | drzewo audio |
| `DOWNLOAD_AUDIO` | `true` | `false` pomija pobieranie audio |
| `SYNC_INTERVAL_MINUTES` | `15` | odstęp między przebiegami |
| `RUN_ONCE` | `false` | jeden przebieg i wyjście |
| `MAX_CONCURRENCY` | `3` | równolegle przetwarzane nagrania |
| `FULL_REFRESH_HOURS` | `24` | co ile ponownie pobrać szczegóły wszystkich nagrań (0 = wyłączone) |
| `TZ` | `UTC` | strefa czasowa w nazwach katalogów |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `LOG_FORMAT` | `json` | `json` albo `console` |
| `LOG_TO_FILE` | `false` | dodatkowo `STATE_DIR/logs/pocket-sync.log` (rotacja 5×10 MB) |

## Układ archiwum

```
dane   /data/meta/pocket/2026/09/2026-09-25_1140_aktualizacja-sql-server-2016-do-2019_desktop_1790329209135_lpmlvi/
           raw.json          pełna odpowiedź API (źródło prawdy)
           transcript.json   segmenty z timestampami (i mówcami, jeśli są)
           transcript.md
           summary.md        wszystkie podsumowania AI
           actions.json      action items
           .meta.json        ścieżka audio względem katalogu audio, SHA-256, rozmiar, hashe plików, wersja narzędzia
       /data/state/pocket-sync.db

audio  /audio/2026/09/2026-09-25_1140_aktualizacja-sql-server-2016-do-2019_desktop_1790329209135_lpmlvi/
           audio.ogg
```

## Jak działa przebieg

1. Sprawdza zapisywalność `META_DIR`, `STATE_DIR` i `AUDIO_DIR` (tylko gdy `DOWNLOAD_AUDIO=true`),
   usuwa pliki `.part` starsze niż 24 h.
2. Pobiera pełną listę nagrań (strony po 100).
3. Do kolejki trafiają nagrania nowe, ze zmienioną pozycją listy (`updated_at`, tytuł, folder, tagi),
   z niekompletnym przetwarzaniem po stronie Pocket, z audio innym niż `done`/`skipped` oraz te,
   których szczegóły nie były pobierane od `FULL_REFRESH_HOURS`. Nagrania jeszcze przetwarzane
   (`state != completed`) czekają.
4. Dla każdego: szczegóły → `raw.json` → audio (świeży presigned URL, `.part`, SHA-256,
   kontrola `Content-Length`, `os.replace` + `fsync`) → pliki pochodne → `.meta.json` → wiersz w bazie.
   Pliki o niezmienionej treści nie są nadpisywane.
5. Błąd jednego nagrania nie przerywa przebiegu. Po 5 kolejnych błędach nagranie jest logowane
   jako `recording_needs_attention` i ponawiane coraz rzadziej (maks. raz na 24 h).

Klient respektuje limit API (50 zapytań/min, nagłówki `X-Ratelimit-*`), ponawia 429/5xx
z wykładniczym backoffem i `Retry-After`, a błędów 4xx nie ponawia.

## Rozwój lokalny

```sh
python -m venv .venv && .venv/bin/pip install -e ".[dev]"   # Windows: .venv\Scripts\pip
.venv/bin/pytest

# przebieg na laptopie bez audio, z kluczem z .env:
META_DIR=./data/meta STATE_DIR=./data/state DOWNLOAD_AUDIO=false LOG_FORMAT=console \
  .venv/bin/python -m pocket_sync once

docker build -t pocket-sync .
```
