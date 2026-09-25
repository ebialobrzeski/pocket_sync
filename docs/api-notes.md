# Pocket Public API: notatki z rekonesansu (krok 0)

Źródła: `https://docs.heypocketai.com/docs/api` oraz ręczne wywołania `curl` prawdziwym kluczem
(2026-09-25, konto z 10 nagraniami). Dokumentacja podaje schematy głównie jako placeholdery
(`"string"`, `null`), więc kształty poniżej pochodzą z rzeczywistych odpowiedzi. Treści są okrojone,
identyfikatory użytkownika zamaskowane.

## Podstawy

- Bazowy adres: `https://public.heypocketai.com/api/v1`
- Uwierzytelnianie: nagłówek `Authorization: Bearer pk_...`
- Koperta odpowiedzi: `{"success": bool, "data": ..., "error": "...", "pagination": {...}}`.
  Przy błędzie `success=false`, `error` zawiera komunikat, `data` bywa pominięte.
- Limity zapytań: nagłówki na każdej odpowiedzi API (nie na S3):
  ```
  X-Ratelimit-Limit: 50
  X-Ratelimit-Remaining: 49
  X-Ratelimit-Reset: 1790333220      # epoch w sekundach, pełna minuta
  ```
  Wygląda na okno 50 zapytań na minutę. Klient zwalnia, gdy `Remaining` spada do 0,
  a przy 429 respektuje `Retry-After` (lub `X-Ratelimit-Reset`, gdy `Retry-After` brak).
- Wszystkie daty w UTC, format `2026-09-25T09:42:13Z`.

### Błędy

| sytuacja | status | body |
|---|---|---|
| zły klucz | 401 | `{"success":false,"error":"API key not found"}` |
| nieznane nagranie | 404 | `{"success":false,"error":"recording not found"}` |
| nagranie bez pliku audio | 404 (na `/audio-url`) | `{"success":false,"error":"recording audio file not found"}` |
| limit | 429 | koperta z `error` (udokumentowane, niezaobserwowane) |

## Endpointy

### `GET /public/recordings` (lista)

Parametry: `page` (od 1), `limit` (domyślnie 20, maks. 100), `start_date` / `end_date`
(`YYYY-MM-DD`, UTC), `tag_ids` (lista po przecinkach). **Paginacja offsetowa po numerze strony**,
brak kursora. **Filtr po dacie dotyczy daty nagrania, nie modyfikacji**: nie ma parametru
„zmienione od”.

```json
{
  "success": true,
  "data": [
    {
      "id": "desktop_1790329209135_lpmlvi",
      "title": "Aktualizacja SQL Server 2016 do 2019",
      "folder_id": null,
      "duration": 109,
      "state": "completed",
      "language": null,
      "recording_at": "2026-09-25T09:40:09Z",
      "created_at": "2026-09-25T09:42:01Z",
      "updated_at": "2026-09-25T09:43:33Z",
      "tags": []
    }
  ],
  "pagination": {"page": 1, "limit": 3, "total": 10, "total_pages": 4, "has_more": true}
}
```

Uwagi:
- Lista **nie zawiera** transkrypcji ani podsumowań (wbrew schematowi w dokumentacji).
- Formaty `id`: `desktop_<ms>_<rand>` (aplikacja desktop), UUID (urządzenie),
  `<userId>_getting_started` (nagranie powitalne).
- `duration` bywa `null` (nagranie powitalne).
- `updated_at` zmienia się przy przeniesieniu do folderu i po wygenerowaniu podsumowania,
  więc jest używalnym znacznikiem modyfikacji. Narzędzie i tak liczy `meta_hash` szczegółów.

### `GET /public/recordings/{id}` (szczegóły)

Parametry: `include_transcript` (domyślnie `true`), `include_summarizations` (domyślnie `true`),
`summarization_id`.

**Transkrypcje i podsumowania są zwracane na tym koncie** (plan nie-Pro), więc `transcript.*`
i `summary.md` są generowane w v1.

```json
{
  "success": true,
  "data": {
    "id": "desktop_1790329209135_lpmlvi",
    "title": "...", "folder_id": null, "duration": 109, "state": "completed",
    "language": null, "recording_at": "...", "created_at": "...", "updated_at": "...",
    "tags": [{"id": "...", "name": "...", "color": "..."}],
    "transcript": {
      "metadata": {"duration": 105.565, "source": "smallest"},
      "segments": [
        {"start": 20.08, "end": 23.04, "text": "...", "originalText": "...", "speaker": "Pocket"}
      ],
      "text": "pełny tekst transkrypcji"
    },
    "summarizations": {
      "c553c6d6-...": {
        "id": "80365862-...",
        "summarizationId": "c553c6d6-...",
        "processingStatus": "completed",
        "v2": {
          "summary": {"markdown": "### Teza\n...", "version": "1"},
          "mindMap": {"type": "flow", "nodes": [{"node_id": "b1", "parent_node_id": "root", "title": "...", "color": "#FF9500"}]},
          "actionItems": {
            "version": "3",
            "actions": [
              {
                "id": "automate_sql_upgrade",
                "globalActionItemId": "8872e929-...",
                "label": "...", "context": "...",
                "assignee": "me", "dueDate": null, "priority": "low", "status": "TODO",
                "isCompleted": false, "is_completed": false,
                "type": "create_reminder", "payload": {"reminder": {"title": "..."}}
              }
            ]
          }
        },
        "settings": {"language": "Polish", "modelId": "...", "...": "dziesiątki pól diagnostycznych"},
        "v2SummaryStatus": {"status": "completed"},
        "v2MindMapStatus": {"status": "completed"},
        "v2ActionItemsStatus": {"status": "completed"},
        "createdAt": "2026-09-25T09:42:28Z",
        "updatedAt": "2026-09-25T09:43:34Z"
      }
    }
  }
}
```

Uwagi:
- `summarizations` to **słownik** `summarizationId → obiekt`, a jedno nagranie może mieć ich
  kilka (zaobserwowano 2). `summary.md` zawiera wszystkie, posortowane po `createdAt`.
- Pole `speaker` w segmentach występuje tylko czasem (brak w nagraniach bez diaryzacji).
- Lista action items bywa pod kluczem `actions`, a w nagraniu powitalnym pod `actionItems`
  (`v2.actionItems.actionItems`). Model obsługuje oba.
- Tagi mają kształt `{id, name, color}`. Na tym koncie nie było żadnych tagów.
- W dokumentacji są jeszcze pola `transcript_error`, `summarizations_errors`, `translation`,
  `translation_error`, ale w rzeczywistych odpowiedziach nie wystąpiły. Modele je dopuszczają.

### `GET /public/recordings/{id}/audio-url`

Parametr `expires_in` (sekundy, 60–86400, domyślnie 3600).

```json
{"success": true, "data": {"signed_url": "https://pocket-recording-dev.s3.us-east-2.amazonaws.com/<user>/2026-09-25/<id>.ogg?X-Amz-...", "expires_in": 3600, "expires_at": "2026-09-25T11:46:35Z"}}
```

Plik z S3 (presigned GET):
- `desktop_*` → `.ogg`, `Content-Type: audio/ogg`; nagrania z urządzenia (UUID) → `.mp3`,
  `Content-Type: audio/mpeg`. **Rozszerzenie brane jest ze ścieżki URL-a**, plik zapisywany
  jako `audio.<ext>`.
- `Content-Length` jest zwracany, `Accept-Ranges: bytes` jest obsługiwane, jest też `ETag`.
- URL wygasa. Wygasły URL S3 zwraca 403 i wtedy narzędzie pobiera nowy URL.
- Nagranie powitalne nie ma pliku (404 `recording audio file not found`) → `audio_status=skipped`.

### `GET /public/folders`

```json
{"success": true, "data": [{"id": "fc0e4996-...", "name": "Work", "kind": "space", "parent_folder_id": null, "space_id": "fc0e4996-...", "color": null, "recording_count": 5, "total_recording_count": 5, "created_at": "...", "updated_at": "...", "children": []}]}
```

Struktura drzewiasta (`children`). Narzędzie spłaszcza ją do ścieżek `Work/Podfolder`
i zapisuje nazwę folderu w `.meta.json`.

### `GET /public/tags`

`{"success": true, "data": []}`. Na tym koncie pusta lista. Tagi są też osadzone w każdym nagraniu.

## Rozstrzygnięcia otwartych kwestii z planu (sekcja 14)

| kwestia | wynik |
|---|---|
| transkrypcje na planie darmowym | **dostępne** przez REST |
| retencja audio | nieudokumentowana. Wszystkie nagrania z ~10 dni były dostępne |
| stabilny znacznik modyfikacji | `updated_at` istnieje i reaguje na zmiany. `meta_hash` pozostaje zabezpieczeniem |
| `Content-Length` przy audio | **tak** (S3), weryfikacja rozmiaru aktywna |
| format audio | `.ogg` / `.mp3`, nie `.m4a` |
| filtr po dacie modyfikacji | **brak**, jest tylko filtr po dacie nagrania |

## Wpływ na implementację

- Lista nie zwraca podsumowań, więc kolejka jest budowana z `list_hash` (hash pozycji listy:
  tytuł, `updated_at`, folder, tagi, stan). Po pobraniu szczegółów liczony jest `meta_hash`
  i gdy się nie zmienił, pliki pochodne nie są nadpisywane. Dodatkowo co `FULL_REFRESH_HOURS`
  (domyślnie 24 h) szczegóły wszystkich nagrań są pobierane ponownie, co wykrywa zmianę
  podsumowania nawet bez zmiany `updated_at`.
- Nagrania ze `state != completed` są pomijane do czasu zakończenia przetwarzania po stronie Pocket,
  żeby nie tworzyć katalogów `untitled`.
- Nagranie jest oznaczane `meta_status=pending` (i sprawdzane w każdym przebiegu), dopóki któreś
  z podsumowań ma `processingStatus` inne niż `completed`/`failed`.
