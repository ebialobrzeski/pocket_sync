# Plan implementacji: `pocket-nas-sync`

Dokument przeznaczony dla coding agenta. Opisuje narzędzie synchronizujące nagrania z Pocket API
do lokalnego archiwum na NAS-ie.

## 1. Cel i zakres

**Cel.** Kontener Dockera działający na NAS-ie (UGREEN DXP4800 Plus), który okresowo odpytuje
Pocket API i utrzymuje kompletne lokalne archiwum nagrań: pliki audio, transkrypcje, podsumowania,
action items i tagi. Narzędzie ma być idempotentne, odporne na restart i nie zostawiać uszkodzonych
plików przy przerwanym transferze.

**Poza zakresem v1.** Odbiornik webhooków, upload do Pocket, interfejs webowy, transkrypcja lokalna,
wyszukiwanie pełnotekstowe. Architektura ma jednak zostawiać miejsce na dołożenie tego później.

**Kluczowe założenie o storage.** Pliki audio trafiają na pulę HDD, wszystko pozostałe (metadane,
transkrypcje, podsumowania, baza stanu, logi) na SSD. Są to dwa niezależne punkty montowania
i narzędzie nigdy nie zakłada, że leżą na tym samym wolumenie.

## 2. Krok 0: rekonesans API (wykonać przed pisaniem kodu)

Nie zgaduj nazw pól. Pobierz `https://docs.heypocketai.com/docs` i spisz dokładne schematy
odpowiedzi. Wynik zapisz jako `docs/api-notes.md` w repozytorium.

Do ustalenia:

- Bazowy adres i sposób uwierzytelniania (spodziewany `https://public.heypocketai.com/api/v1`,
  nagłówek `Authorization: Bearer pk_...`).
- Endpointy: lista nagrań, szczegóły nagrania, URL do pobrania audio, lista folderów, lista tagów.
- Sposób paginacji (cursor czy offset/limit) oraz nazwa parametru filtrowania po dacie modyfikacji.
- Czy szczegóły nagrania zwracają transkrypcję i podsumowanie na planie darmowym, czy tylko Pro.
  To zmienia sens narzędzia: jeśli tylko Pro, v2 będzie musiała transkrybować lokalnie.
- Format URL-a do audio (najpewniej presigned z krótkim TTL), rozszerzenie i typ MIME pliku.
- Kształt action items i tagów w odpowiedzi.
- Limity zapytań, jeśli są udokumentowane; jeśli nie, założyć konserwatywne.

Jeśli dokumentacja jest niejednoznaczna, wykonać ręczny `curl` prawdziwym kluczem i wkleić
okrojoną odpowiedź do `docs/api-notes.md`.

## 3. Stos technologiczny

- Python 3.12
- `httpx` (klient HTTP, streaming pobierania)
- `pydantic` + `pydantic-settings` (walidacja odpowiedzi API i konfiguracji)
- `tenacity` (retry z backoffem)
- SQLite ze standardowej biblioteki (stan)
- `structlog` lub `logging` z formatterem JSON
- `pytest`, `respx` (testy)
- Obraz bazowy `python:3.12-slim`, build wieloetapowy, kontener uruchamiany jako nie-root

## 4. Układ repozytorium

```
pocket-sync/
  pyproject.toml
  Dockerfile
  docker-compose.yml
  .env.example
  docs/
    api-notes.md          # wynik kroku 0
  src/pocket_sync/
    __main__.py           # pętla albo tryb jednorazowy
    config.py             # ustawienia z ENV
    api.py                # klient API: auth, paginacja, retry
    models.py             # modele pydantic
    state.py              # SQLite: schemat, migracje, zapytania
    paths.py              # rozwiązywanie ścieżek HDD/SSD, slugi
    writer.py             # atomowy zapis plików
    sync.py               # orkiestracja jednego przebiegu
    verify.py             # weryfikacja spójności archiwum
  tests/
```

## 5. Podział storage

Dwa niezależne drzewa o **identycznej strukturze katalogów względnych**. Klucz relacji to ścieżka
względna, nie symlink.

### 5.1. SSD — `META_DIR` (domyślnie `/data/meta`)

```
/data/meta/pocket/2026/09/2026-09-25_1009_plany-przyjazdu_<id>/
    raw.json           # pełna odpowiedź API — źródło prawdy
    transcript.json    # segmenty z timestampami i mówcami
    transcript.md      # czytelna wersja tekstowa
    summary.md         # podsumowanie AI w markdown
    actions.json       # action items
    .meta.json         # hashe, czasy, wersja narzędzia, ścieżka audio
```

Dodatkowo na SSD:

```
/data/state/pocket-sync.db     # baza stanu (SQLite, tryb WAL)
/data/state/logs/              # opcjonalne logi na dysku
```

### 5.2. HDD — `AUDIO_DIR` (domyślnie `/mnt/hdd/pocket-audio`)

```
/mnt/hdd/pocket-audio/2026/09/2026-09-25_1009_plany-przyjazdu_<id>/
    audio.m4a
```

### 5.3. Zasady

- Ścieżka względna (`2026/09/<slug>_<id>/`) jest wyliczana raz, w `paths.py`, i używana w obu
  drzewach. Funkcja `relative_dir(recording) -> Path` to jedyne miejsce, gdzie powstaje ta ścieżka.
- `.meta.json` zawiera pole `audio_path` z **bezwzględną ścieżką hosta** do pliku audio oraz
  `audio_sha256` i `audio_bytes`. Dzięki temu archiwum pozostaje czytelne nawet bez bazy stanu.
- Symlinki między drzewami są opcjonalne i domyślnie wyłączone (`CREATE_AUDIO_SYMLINK=false`).
  Przy dostępie przez SMB symlinki między różnymi udziałami bywają nieprzenośne.
- Slug tytułu: transliteracja do ASCII (polskie znaki), lowercase, spacje na `-`, maks. 60 znaków.
  Pusty tytuł zastępuje `untitled`.
- Narzędzie tworzy katalogi w obu drzewach z `parents=True, exist_ok=True` i sprawdza przy starcie,
  że oba są zapisywalne. Brak dostępu do któregokolwiek to błąd krytyczny, nie ostrzeżenie.
- `DOWNLOAD_AUDIO=false` wyłącza całą gałąź HDD, przydatne przy testach na laptopie.

## 6. Stan (SQLite)

Plik `${STATE_DIR}/pocket-sync.db`, tryb WAL, `synchronous=NORMAL`.

### Tabela `recordings`

| kolumna | typ | opis |
|---|---|---|
| `id` | TEXT PK | identyfikator nagrania z API |
| `created_at` | TEXT | data nagrania (ISO 8601) |
| `updated_at_remote` | TEXT | znacznik modyfikacji z API, jeśli dostępny |
| `title` | TEXT | tytuł z API |
| `rel_dir` | TEXT | ścieżka względna wspólna dla obu drzew |
| `meta_status` | TEXT | `pending` / `done` / `failed` |
| `meta_hash` | TEXT | SHA-256 kanonicznego JSON-a metadanych |
| `audio_status` | TEXT | `pending` / `done` / `failed` / `skipped` |
| `audio_sha256` | TEXT | suma kontrolna pobranego pliku |
| `audio_bytes` | INTEGER | rozmiar pliku |
| `last_synced_at` | TEXT | czas ostatniego udanego zapisu |
| `error_count` | INTEGER | licznik kolejnych błędów |
| `last_error` | TEXT | ostatni komunikat błędu |

### Tabela `sync_runs`

`id`, `started_at`, `finished_at`, `processed`, `skipped`, `failed`, `bytes_downloaded`, `status`.

### Tabela `schema_version`

Jedna kolumna `version`. Migracje w `state.py` jako lista funkcji wykonywanych po kolei.

`meta_hash` wykrywa zmianę podsumowania niezależnie od `updated_at_remote`, którego zachowania
po stronie Pocket jeszcze nie znamy.

## 7. Algorytm jednego przebiegu

1. Sprawdź dostępność i zapisywalność `META_DIR`, `STATE_DIR` oraz `AUDIO_DIR`
   (to ostatnie tylko gdy `DOWNLOAD_AUDIO=true`).
2. Pobierz wszystkie strony listy nagrań. W v1 pełna lista; filtr po dacie dołożyć, gdy archiwum
   przekroczy kilkaset pozycji.
3. Porównaj ze stanem. Do kolejki trafiają nagrania nieznane, ze zmienionym `meta_hash`
   oraz te z `audio_status` różnym od `done` i `skipped`.
4. Dla każdej pozycji z kolejki:
   1. `GET` szczegółów nagrania.
   2. Zapisz `raw.json` na SSD — zawsze, nawet jeśli walidacja modeli pydantic się nie powiedzie.
   3. **Pobierz audio na HDD** (patrz 7.1). Audio ma priorytet: jest nieodtwarzalne, a reszta
      wynika z `raw.json`.
   4. Wygeneruj `transcript.json`, `transcript.md`, `summary.md`, `actions.json` na SSD.
   5. Zapisz `.meta.json` ze ścieżką audio i sumami kontrolnymi.
   6. Zaktualizuj wiersz w `recordings` w jednej transakcji.
5. Błąd pojedynczego nagrania inkrementuje `error_count`, zapisuje `last_error` i **nie przerywa
   przebiegu**. Po 5 kolejnych nieudanych próbach nagranie jest logowane jako wymagające uwagi,
   ale nadal ponawiane w kolejnych przebiegach z rzadszą częstotliwością.
6. Zapisz wiersz w `sync_runs` i wypisz podsumowanie przebiegu do logu.

### 7.1. Pobieranie audio

- URL pobierz **bezpośrednio przed transferem**, nigdy nie zapisuj go w bazie — jest presigned
  i wygasa.
- Streamuj do `audio.m4a.part` w katalogu docelowym na HDD.
- Licz SHA-256 w locie.
- Po zakończeniu porównaj rozmiar z `Content-Length`. Niezgodność to błąd, plik `.part` usuń.
- `os.replace()` na nazwę finalną, następnie `fsync` na deskryptorze katalogu.
- Osierocone pliki `.part` starsze niż 24 h są kasowane na starcie przebiegu.

### 7.2. Współbieżność i retry

- Semafor na `MAX_CONCURRENCY` (domyślnie 3) równoległych pobierań.
- Retry: wykładniczy backoff z jitterem na 429 i 5xx, maksymalnie 5 prób, nagłówek `Retry-After`
  respektowany.
- 4xx poza 429 to błąd trwały — bez ponawiania w tym przebiegu.
- Timeouty `httpx`: 10 s connect, 30 s read dla API, 300 s read dla transferu audio.

## 8. Konfiguracja (zmienne środowiskowe)

| zmienna | domyślna | opis |
|---|---|---|
| `POCKET_API_KEY` | — | klucz `pk_...`, wymagany |
| `POCKET_API_BASE` | `https://public.heypocketai.com/api/v1` | bazowy adres API |
| `META_DIR` | `/data/meta/pocket` | drzewo metadanych na SSD |
| `STATE_DIR` | `/data/state` | baza stanu i logi, SSD |
| `AUDIO_DIR` | `/mnt/hdd/pocket-audio` | drzewo audio na HDD |
| `DOWNLOAD_AUDIO` | `true` | wyłączenie pomija całą gałąź HDD |
| `CREATE_AUDIO_SYMLINK` | `false` | symlink z katalogu meta do pliku audio |
| `SYNC_INTERVAL_MINUTES` | `15` | odstęp między przebiegami |
| `RUN_ONCE` | `false` | jeden przebieg i wyjście (tryb cron/debug) |
| `MAX_CONCURRENCY` | `3` | równoległe pobierania |
| `LOG_LEVEL` | `INFO` | poziom logowania |
| `LOG_FORMAT` | `json` | `json` albo `console` |

Klucz API przekazywany przez Docker secret albo plik `.env` z uprawnieniami `600`. Nigdy w
`docker-compose.yml` commitowanym do repo. W repo tylko `.env.example` z pustymi wartościami.

## 9. Docker

`docker-compose.yml`, jeden serwis:

- `restart: unless-stopped`
- `user: "1000:1000"` — dopasować do właściciela udziałów na NAS-ie
- dwa bind mounty: udział SSD na `/data`, udział HDD na `/mnt/hdd/pocket-audio`
- `env_file: .env`
- healthcheck: sprawdza, czy ostatni udany wiersz w `sync_runs` jest młodszy niż
  3 × `SYNC_INTERVAL_MINUTES`; jeśli nie, kontener raportuje `unhealthy`
- limity pamięci nieobowiązkowe, narzędzie jest lekkie; streaming pobierania nie trzyma pliku w RAM

`Dockerfile`: etap budujący z `pip install`, etap finalny bez kompilatorów, `ENTRYPOINT ["python",
"-m", "pocket_sync"]`.

## 10. Weryfikacja archiwum (`verify.py`)

Osobne polecenie uruchamiane ręcznie, nie w pętli. Sprawdza:

- każdy wiersz z `audio_status='done'` ma istniejący plik na HDD o zgodnym rozmiarze,
- opcjonalnie (`--checksums`) przelicza SHA-256 i porównuje ze stanem,
- każdy katalog na HDD ma odpowiadający katalog na SSD i odwrotnie — wykrywa rozjazd między
  drzewami po ręcznych operacjach na plikach,
- raportuje osierocone pliki `.part`.

Wyjście: czytelny raport plus kod wyjścia różny od zera przy wykrytych problemach, żeby dało się
podpiąć pod cron.

## 11. Testy

- **Klient API** (`respx`): paginacja wielostronicowa, 429 z `Retry-After`, 500 z retry,
  wygasły URL audio, odpowiedź z nieznanym dodatkowym polem.
- **Ścieżki**: transliteracja polskich znaków, obcinanie długich tytułów, pusty tytuł,
  identyczna ścieżka względna w obu drzewach.
- **Writer**: przerwane pobieranie nie zostawia pliku finalnego; kolejny przebieg dokańcza pracę;
  niezgodny `Content-Length` powoduje usunięcie `.part`.
- **Sync**: drugi przebieg na tych samych danych nie generuje ruchu sieciowego (idempotencja);
  zmiana podsumowania po stronie API nadpisuje `summary.md`, ale nie pobiera audio ponownie;
  błąd jednego nagrania nie przerywa pozostałych.
- **Rozdzielne storage**: test z `AUDIO_DIR` na innym tmpdirze niż `META_DIR` sprawdza, że żadna
  ścieżka nie jest wyliczana względem drugiego drzewa.
- **Stan**: migracja z pustej bazy, ponowne uruchomienie migracji jest nieszkodliwe.

## 12. Kolejność prac

1. Krok 0 (rekonesans API) plus szkielet repo, `config.py`, `.env.example`.
2. `api.py` z paginacją i retry oraz jego testy.
3. `models.py` na podstawie `docs/api-notes.md`.
4. `state.py`: schemat, migracje, zapytania.
5. `paths.py` i `writer.py` z atomowym zapisem i rozdziałem HDD/SSD.
6. `sync.py`, tryb `RUN_ONCE`, logowanie.
7. `verify.py`.
8. `Dockerfile`, `docker-compose.yml`, uruchomienie na NAS-ie.
9. Dopiero potem: backfill z filtrem daty, odbiornik webhooka, ewentualna transkrypcja lokalna.

## 13. Kryteria akceptacji

- Przebieg na pustym archiwum kończy się kodem 0 i nie tworzy śmieci.
- Po pierwszym nagraniu: `audio.m4a` leży na HDD, komplet plików tekstowych na SSD, obie ścieżki
  mają tę samą część względną, a `audio_sha256` w bazie zgadza się z sumą pliku na dysku.
- Drugi przebieg nie pobiera niczego i loguje `skipped`.
- Ubicie kontenera w trakcie pobierania nie zostawia uszkodzonego `audio.m4a`; kolejny przebieg
  dokańcza pracę.
- Uruchomienie z `DOWNLOAD_AUDIO=false` nie dotyka `AUDIO_DIR` i nie wymaga jego istnienia.
- `verify.py` na zdrowym archiwum zwraca kod 0; po ręcznym usunięciu pliku audio zwraca błąd
  i wskazuje konkretne nagranie.
- Brak klucza API kończy się czytelnym komunikatem, nie stacktrace'em.

## 14. Otwarte kwestie do rozstrzygnięcia w kroku 0

- Czy transkrypcje są dostępne przez REST API na planie darmowym. Jeśli nie, `transcript.*` staje
  się opcjonalne, a w v2 dochodzi lokalny Whisper.
- Jaka jest rzeczywista retencja audio w chmurze na planie darmowym. Wpływa na to, jak agresywnie
  musi działać pierwszy backfill.
- Czy API udostępnia stabilny znacznik modyfikacji. Jeśli nie, `meta_hash` pozostaje jedynym
  mechanizmem wykrywania zmian.
- Czy `Content-Length` jest zwracany przy pobieraniu audio. Jeśli nie, weryfikacja rozmiaru odpada
  i zostaje sama suma kontrolna.
