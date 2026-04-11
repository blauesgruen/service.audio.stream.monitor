# ASM <-> ASM-QF Shared DB Contract

Stand: 2026-04-11

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
