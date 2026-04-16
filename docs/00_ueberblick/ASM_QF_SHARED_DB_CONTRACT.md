# ASM <-> ASM-QF Shared DB Contract

Stand: 2026-04-16

## Ziel

ASM (`service.audio.stream.monitor`) und ASM-QF (`service.audio.stream.monitor.qf`) teilen eine gemeinsame SQLite-DB fuer verifizierte Senderquellen.

Pfad:

`~userdata/addon_data/service.audio.stream.monitor/song_data.db`

## Tabelle

Die Tabelle wird von ASM angelegt und bei Start migriert:

```sql
CREATE TABLE IF NOT EXISTS verified_station_sources (
    station_key       TEXT NOT NULL,
    station_name      TEXT NOT NULL DEFAULT '',
    station_name_norm TEXT NOT NULL DEFAULT '',
    source_url        TEXT NOT NULL,
    source_url_norm   TEXT NOT NULL,
    source_kind       TEXT NOT NULL DEFAULT 'stream',
    verified_by       TEXT NOT NULL DEFAULT '',
    confidence        REAL NOT NULL DEFAULT 1.0,
    verified_at_utc   TEXT NOT NULL DEFAULT '',
    last_seen_ts      INTEGER NOT NULL DEFAULT 0,
    meta_json         TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (station_key, source_url_norm)
);
CREATE INDEX IF NOT EXISTS idx_verified_sources_url_norm
ON verified_station_sources(source_url_norm);
CREATE INDEX IF NOT EXISTS idx_verified_sources_station_norm
ON verified_station_sources(station_name_norm);
```

## Feldvertrag

- `station_key`: stabiler ASM-Station-Key (z. B. `radiode:<slug>`, `tunein:<id>`, `stream:<slug>`, `name:<normalized name>`).
- `station_name`: lesbarer Name.
- `station_name_norm`: lower+trim+single-space Form von `station_name`.
- `source_url`: originale Stream-URL.
- `source_url_norm`: lower+trim Form von `source_url`.
- `source_kind`: Herkunft der Verifikation (z. B. `radiode_api`, `icy_header`, `qf_verified`).
- `verified_by`: Addon-ID des Writers (z. B. `service.audio.stream.monitor.qf`).
- `confidence`: `0.0 .. 1.0`.
- `verified_at_utc`: ISO UTC Timestamp (`YYYY-MM-DDTHH:MM:SSZ`).
- `last_seen_ts`: Unix timestamp (sek).
- `meta_json`: optionales JSON mit Zusatzdetails.

## Upsert-Regel

Der Primarschluessel ist `(station_key, source_url_norm)`.
Neue Writes auf denselben Key aktualisieren den Datensatz (confidence, timestamps, meta).

## ASM-Leselogik

- ASM kann ueber `source_url_norm` einen Treffer holen.
- Wenn `station_name` vorhanden ist und ASM noch keinen Stationsnamen hat, wird der Name als Hint gesetzt.
- Debug-Properties:
  - `RadioMonitor.VerifiedSourceUrl`
  - `RadioMonitor.VerifiedSourceBy`
  - `RadioMonitor.VerifiedSourceConfidence`

## Empfehlung fuer ASM-QF Writes

Minimaler Upsert pro verifizierter Quelle:

- `station_key`
- `station_name`
- `station_name_norm`
- `source_url`
- `source_url_norm`
- `source_kind='qf_verified'`
- `verified_by='service.audio.stream.monitor.qf'`
- `confidence` (z. B. `0.95`)
- `verified_at_utc` / `last_seen_ts`
- `meta_json` (optional)

## Parallelzugriff

- SQLite in WAL-Mode ist aktiv.
- Leser und Schreiber koennen parallel arbeiten.

## Nicht-Ziele dieses Vertrags

- QF-Request/Response-Properties (`RadioMonitor.QF.*`) sind Teil des Runtime-Window-Property-Protokolls, nicht Teil dieses DB-Vertrags.
- Neue Diagnose-/Hold-Logs (`ASM-QF DIAG event=...`, `hold_*`, `non_fresh`) aendern das DB-Schema nicht.

## Ergaenzender Runtime-Property-Vertrag (ASM <-> ASM-QF)

Dieser Abschnitt beschreibt den Laufzeitvertrag fuer `RadioMonitor.QF.*` zusaetzlich zum DB-Vertrag.
Er aendert kein DB-Schema, ist aber fuer stabile Source-Entscheidungen verpflichtend.

### Request -> genau eine terminale Response

- Jede von ASM gesetzte `RadioMonitor.QF.Request.Id` MUSS in endlicher Zeit genau eine terminale Response erzeugen.
- Die terminale Response MUSS `RadioMonitor.QF.Response.Id` auf die betroffene Request-ID setzen.
- Zulaessige terminale Statuswerte:
  - `hit`
  - `no_hit`
  - `resolve_error`
  - `error`
  - `timeout`
  - `superseded`
  - `cancelled`

### Mindestfelder pro Response

- `RadioMonitor.QF.Response.Id`
- `RadioMonitor.QF.Response.Status`
- `RadioMonitor.QF.Response.Ts`
- Bei `status=hit` zusaetzlich: `RadioMonitor.QF.Response.Artist`, `RadioMonitor.QF.Response.Title`

Hinweis fuer Skin-Labels:
- ASM setzt `RadioMonitor.QF.Response.StationUsed` aus `RadioMonitor.QF.Response.Meta.station_used` (bei frischer Response).
- Uebliche `stationused`-Labels koennen weiterhin auf `RadioMonitor.QF.Response.Source` und/oder `RadioMonitor.QF.Response.Meta` mappen.

### Supersede/Cancellation-Regel

- Wenn eine laufende Anfrage intern durch eine neuere Anfrage abgeloest wird, MUSS die alte Anfrage explizit abgeschlossen werden (`status=superseded` oder `status=cancelled`).
- Ein stilles Verwerfen ohne Response ist nicht erlaubt, weil ASM sonst bis zum Fallback-Fenster (`QF_NO_RESPONSE_FALLBACK_S`) in einem no-response-Wartezustand bleibt.

### Freshness/Diagnose (ASM-Seite)

- ASM bewertet Freshness primaer ueber Request-ID-Match; non-fresh ist nicht automatisch ein Fehler.
- Fuer Diagnosen sind zentral die Felder `fresh_reason`, `gap_source`, `gap_s` aus `ASM-QF DIAG event=non_fresh` massgeblich.
- Autoritative no-hit-Phasen koennen kurz gepuffert sein (`QF_NO_HIT_HOLD_S`), damit transiente no-hit-Responses Labels nicht sofort leeren.
- Frische Hit-Paare werden in ASM kurzfristig gelatcht (`_last_qf_fresh_hit_pair`), damit kurze Poll-Races den QF-Wechsel-Trigger nicht verlieren.
- Bei langen terminalen QF-Entscheidungen kann um `QF_NO_RESPONSE_FALLBACK_S` (~25s) kurz `fresh_reason=stale_response` auftreten; das ist ein Timing-Indikator fuer die QF-Kette.
- Im `asm-qf*`-Entscheidungspfad werden Artist/Title paar-atomar behandelt: unvollstaendige Paare loeschen beide Label-Felder statt halber Updates.

### Stabile Request-Station (ASM-Seite)

- ASM verwendet pro Stream-Session einen stabilen Request-Anchor fuer `RadioMonitor.QF.Request.Station`.
- Der Anchor wird bei erster valider Station gesetzt und erst bei echtem Streamwechsel/Stop zurueckgesetzt.
- Dadurch fuehren spaetere sichtbare Stationsnamen-Varianten nicht zu wechselnden QF-Request-Stationen.

