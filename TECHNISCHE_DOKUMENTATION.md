# Technische Dokumentation - Audio Stream Monitor

Diese Datei dokumentiert den internen Laufzeitfluss des Addons fuer Wartung, Debugging und sichere Refactorings.
Die `README.md` bleibt der Nutzer-/Feature-Einstieg, diese Datei ist die Maintainer-Sicht.

## 1) Ziel und Scope

Das Service-Addon beobachtet aktive HTTP/HTTPS-Audio-Streams in Kodi und schreibt normalisierte Metadaten in
Window-Properties `RadioMonitor.*`.

Kernziele:
- robuste Erkennung von Artist/Title aus ICY, radio.de API, TuneIn API und Kodi MusicPlayer
- Validierung/Anreicherung ueber MusicBrainz (MBID, Album, FirstRelease, Genre, Banddaten)
- stabile Property-Updates fuer konsumierende Skins/Addons (insb. Artist Slideshow Trigger-Verhalten)

## 2) Module und Verantwortung

- `service.py`
  - Runtime-Orchestrierung, Worker-Threads, Fallback-Reihenfolge, Property-Write/Clear-Logik
- `metadata.py`
  - ICY `StreamTitle` Extraktion, Trennung Artist/Title, Trennzeichenlogik, Titel-Bereinigung, Artist-Varianten
- `musicbrainz.py`
  - Matching-Entscheidungen, Recording/Artist Lookups, Album-Auswahl, Song-Kontext-Aufloesung, Similarity
- `radiode.py`
  - Parsing des radio.de now-playing Titelformats
- `cache.py`
  - Thread-safe MB Song-Cache mit TTL
- `api_client.py`
  - HTTP-Client mit Retry + Exponential-Backoff
- `constants.py`
  - Endpunkte, Header, Timeouts, Property-Namen, Regex-Konstanten
- `logger.py`
  - zentrales Logging-Praefix

## 3) Laufzeitarchitektur (High-Level)

Startpunkt:
- `service.py` -> `if __name__ == '__main__': RadioMonitor().run()`

Event-/Polling-Quellen:
- `PlayerMonitor` (Kodi `xbmc.Player`):
  - `onPlayBackStarted()`: versucht frueh `plugin_slug` aus radio.de-light Plugin-URL zu extrahieren
  - zentrale API-Source-Erkennung (Whitelist):
    - `plugin.audio.radiode`
    - `plugin.audio.radio_de_light`
    - `plugin.audio.tunein2017`
  - `onAVStarted()`: behandelt Video/Lokaldateien als harte Stop-Szenarien und liest frueh Logo aus `ListItem.Icon`
- `RadioMonitor.run()`:
  - Polling alle 2 Sekunden (`check_playing()`)

Worker:
- `metadata_worker(url, generation)` fuer ICY-Streams
- `api_metadata_worker(generation)` als Fallback bei fehlendem ICY (radio.de / TuneIn)
- `_musicplayer_metadata_fallback(generation)` als Fallback ohne ICY und ohne API-Basis

## 4) Zustandsmodell und Thread-Sicherheit

Wichtige Runtime-Felder in `RadioMonitor`:
- `is_playing`, `current_url`
- `metadata_thread`, `stop_thread`
- `metadata_generation` (stale worker invalidation)
- `use_api_fallback`, `station_slug`, `plugin_slug`, `station_logo`
- `_last_song_time`, `_song_timeout` (song timeout management)

Thread-Kontrolle:
- `start_metadata_monitoring()` stoppt zuerst alten Worker, inkrementiert `metadata_generation`, startet neuen Thread
- `stop_metadata_monitoring()` setzt `stop_thread=True`, inkrementiert `metadata_generation`, `join(timeout=0.5)`
- Jeder Worker prueft `generation == self.metadata_generation` und beendet sich bei Mismatch

## 5) Stream-Lebenszyklus

### 5.1 Erkennung neuer Wiedergabe

`check_playing()`:
- ignoriert Video und Nicht-HTTP(S)
- bei URL-Wechsel:
  - setzt neuen Stream-Status
  - leert initial `MBID`, `Album`, `Station`
  - liest initiale Player-Tags
  - versucht Logo-Quellen in Prioritaet:
    1. `ListItem.Icon`
    2. `Window(Home).Property(RadioDE.StationLogo)`
    3. `Player.Art(poster)`, `Player.Icon`, `Player.Art(thumb)`, `MusicPlayer.Cover`
  - setzt nur vorlaeufig `RadioMonitor.Title` (bewusst noch kein Artist/Album Trigger)
  - setzt `RadioMonitor.Playing=true`
  - startet `start_metadata_monitoring(playing_file)`

### 5.2 Stop-/Wechsel-Szenarien

Bei Video, lokaler Datei, Stream-Ende oder Addon-Ende:
- `stop_metadata_monitoring()`
- `clear_properties()`

`clear_properties()` loescht:
- radio.de Window-Properties (`RadioDE.StationLogo`, `RadioDE.StationName`)
- alle `RadioMonitor.*` Properties
- interne Logo/Slug-Zwischenwerte

## 6) Metadaten-Pipeline und Fallback-Kette

### 6.1 ICY-Pfad (primaer)

`metadata_worker()` -> `parse_icy_metadata(url)`:
- Request mit Header `Icy-MetaData: 1`
- liest `icy-name`, `icy-genre`, `icy-metaint`
- wenn `icy-metaint` fehlt:
  - `_setup_api_fallback_from_url(url)`
  - kein ICY-Worker, stattdessen Fallback-Worker

Im Loop:
- liest Audio-Bytes bis `metaint`, dann Metadatenblocklaenge und Metadatenblock
- extrahiert `StreamTitle` via `metadata.extract_stream_title`
- bei Titelwechsel:
  - invalidiert MB Song-Cache: `_mb_cache.clear()`
  - parst Artist/Title via `parse_stream_title(stream_title, station_name, url)`

### 6.2 parse_stream_title() Prioritaet

1. API zuerst:
- `get_nowplaying_from_apis(station_name, stream_url)`
- API-Ergebnis wird nur akzeptiert wenn:
  - ICY fehlt/numerisch oder
  - API und ICY-Kombitext aehnlich genug (`mb_similarity >= 0.9`)
- API-Ergebnis wird danach zusaetzlich ueber MB verifiziert/normalisiert

2. ICY-Analyse:
- `metadata.parse_stream_title_complex()`
- erkennt `"Title" von Artist`, mehrere Separatoren, Station-/Invalid-Werte
- numerische Formate wie `123 - 456` gelten als "kein ICY"

3. Mehrfach-Separator-Heuristik:
- bei mehrfach ` - ` zuerst last-separator-Variante pruefen

4. MB-Entscheidung:
- `identify_artist_title_via_musicbrainz(...)`
- bei `uncertain=True` werden ICY-Werte behalten und MB-Felder geleert

### 6.3 API-Fallback-Worker

`api_metadata_worker()` (Intervall 10s):
- aktiviert nur wenn `api_source` aus der Whitelist stammt und `use_api_fallback` oder `plugin_slug` oder `tunein_station_id` vorhanden
- holt Titel via `get_nowplaying_from_apis()`
- bei Artist+Title:
  - MB-Validierung/Anreicherung
  - MB-Felder nur behalten wenn MB zu API-Titel plausibel passt
- bei nur Title:
  - Artist/MBID werden bewusst nicht aggressiv geloescht (stabileres AS-Verhalten)

### 6.4 MusicPlayer-Fallback-Worker

`_musicplayer_metadata_fallback()` (Intervall 5s):
- fuer Streams ohne ICY und ohne API-Basis
- pollt `MusicInfoTag` Artist/Title auf Aenderung
- bei Aenderung:
  - MB-Recording-Query fuer Normalisierung + MB-Felder
  - aktualisiert Logo optional aus `Player.Icon` (z.B. AzuraCast per-song Cover)
- wenn keine verwertbaren Artist/Title Daten mehr vorhanden: deaktiviert `Playing`

## 7) Property-Contract (kritisch)

Die Property-Reihenfolge ist absichtlich.

Wichtig:
- `RadioMonitor.Artist` wirkt als Trigger fuer Artist Slideshow
- deshalb muss `RadioMonitor.MBID` vorher gesetzt sein
- initial bei Streamstart wird Artist bewusst noch nicht gesetzt

Typische Setz-Reihenfolge im Metadatenpfad:
1. `Title`
2. `Album`
3. `AlbumDate`
4. `MBID`
5. `FirstRelease`
6. `Artist` (Trigger)
7. `Logo`
8. optional spaeter `BandFormed`, `BandMembers`, `Genre`

Bei ungueltigen/leerem Titel werden Song-bezogene Felder geloescht:
- `Artist`, `Title`, `Album`, `AlbumDate`, `MBID`, `FirstRelease`, `BandFormed`, `BandMembers`, `Genre`, `StreamTitle`

## 8) Song-Timeout

Nach jedem gueltigen Titelupdate:
- `_last_song_time = now`
- `_song_timeout = duration_ms/1000 + SONG_TIMEOUT_BUFFER_S` wenn MB-Laenge bekannt
- sonst Fallback `SONG_TIMEOUT_FALLBACK_S`

Wenn Timeout ablaeuft:
- Song-bezogene Properties werden geloescht, damit keine stale Titel stehen bleiben

## 9) MusicBrainz-Logik (Kernpunkte)

### 9.1 Query-Strategie Artist/Title-Reihenfolge

`_identify_artist_title_via_musicbrainz(part1, part2)`:
- Q1: `recording:part1 AND artistname:part2`
- Q2: `recording:part2 AND artistname:part1`
- Q3 Fallback (nur Titel) wenn Q1+Q2 keine Treffer liefern
- Entscheidung ueber kombinierten Wert: `MB score * artist similarity`
- Schwellwerte:
  - `MIN_SCORE = 85`
  - `THRESHOLD = 0.7`

### 9.2 Album-/FirstRelease-Auswahl

`_musicbrainz_resolve_song_context(...)`:
- nutzt Work-Relations des gewaehlten Recordings
- sammelt passende Work-Recordings (Paging begrenzt)
- filtert auf erwarteten Artist
- bestimmt Song-FirstRelease ueber passende Recordings
- zieht Releases nach und waehlt fruehes passendes echtes Album

### 9.3 Caches

- `_mb_cache`: Song-Cache (TTL aus `MB_SONG_CACHE_TTL`, default 24h), Key `(title_lower, artist_lower)`
- `_artist_info_cache`: In-Memory Cache fuer Artist-Infos ohne TTL
- bei StreamTitle-Aenderung im ICY-Worker: `_mb_cache.clear()` zur Vermeidung stale MBIDs

## 10) radio.de Integration

`get_radiode_api_nowplaying(station_name)`:
- bevorzugt Slug aus Plugin-URL (`plugin_slug`), alternativ aus Logo-URL
- Details-API kann kanonischen Stationsnamen + Logo setzen
- now-playing API erst ueber Slug/Station-ID, sonst Search-API mit Match-Scoring

TuneIn:
- TuneIn-ID-Erkennung aus Plugin-URL (`plugin.audio.tunein2017`) oder Stream-URL
- API-Queries ueber OPML-Endpunkte (`Describe.ashx`, `Tune.ashx`)
- Kandidatenfelder wie `playing`, `subtext`, `subtitle`, `title` werden heuristisch geparst

API-Restriktion:
- Keine radio.de- oder TuneIn-API-Calls fuer andere Addons/Streams
- Steuerung zentral ueber `api_source` + Helper (`_is_api_source_allowed`, `_can_use_radiode_api`, `_can_use_tunein_api`)

Schutzlogik:
- numerische Titel werden verworfen
- invalid/station-name-only Werte werden gefiltert

## 11) Bekannte Invarianten fuer Refactoring

Nicht verletzen ohne expliziten Grund:
- Property-Setzreihenfolge (insb. MBID vor Artist)
- getrennte, aehnlich aussehende Property-Bloecke nicht blind zusammenfuehren
- numerische ICY-Titel als "kein ICY" behandeln
- MB-Cache bei StreamTitle-Wechsel invalidieren
- `RadioMonitor.Playing` nur in klaren Szenarien setzen/loeschen

## 12) Debugging-Playbook

1. Kodi Debug-Logging aktivieren.
2. In `kodi.log` nach `[Audio Stream Monitor]` filtern.
3. Erwartete Kernmarker:
   - `STREAM GESTARTET - INITIAL STATE`
   - `ICY METADATA (ROH)`
   - `Neuer StreamTitle erkannt`
   - `MB-Cache invalidiert wegen Titelwechsel`
   - `MusicBrainz Entscheidung`
   - `Song-Timeout abgelaufen`

## 13) Erweiterungspunkte

Sichere Erweiterungen:
- neue API-Quelle in `get_nowplaying_from_apis()` integrieren
- neue Normalisierungen in `metadata.py` ergaenzen
- zusaetzliche MB-Heuristiken in `musicbrainz.py` hinter bestehende Schwellenwerte legen

Vorher pruefen:
- beeinflusst die Aenderung Artist/MBID Trigger-Reihenfolge?
- kann sie stale Properties bei Streamwechsel erzeugen?
- bleibt Fallback-Kette (ICY -> API -> MusicPlayer) konsistent?
