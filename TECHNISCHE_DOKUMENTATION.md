# Technische Dokumentation - Audio Stream Monitor

Diese Datei dokumentiert den internen Laufzeitfluss des Addons fuer Wartung, Debugging und sichere Refactorings.
Die `README.md` bleibt der Nutzer-/Feature-Einstieg, diese Datei ist die Maintainer-Sicht.

## 1) Ziel und Scope

Das Service-Addon beobachtet aktive HTTP/HTTPS-Audio-Streams in Kodi und schreibt normalisierte Metadaten in
Window-Properties `RadioMonitor.*`.

Kernziele:
- robuste Erkennung von Artist/Title aus ICY, API (whitelisted) und Kodi MusicPlayer
- Validierung/Anreicherung ueber MusicBrainz (MBID, Album, FirstRelease, Genre, Banddaten)
- stabile Property-Updates fuer konsumierende Skins/Addons (insb. Artist Slideshow Trigger-Verhalten)
- Vermeidung stale Labels durch klares Timeout- und Clearing-Verhalten

Wichtig:
- Das Addon erfindet keine frei erfundenen Metadaten.
- Es waehlt/validiert Quellen und normalisiert Formate, schreibt aber keine "kanonischen Umbenennungen" um jeden Preis.

## 2) Module und Verantwortung

- `service.py`
  - Runtime-Orchestrierung, Worker-Threads, Fallback-Reihenfolge, Property-Write/Clear-Logik
- `metadata.py`
  - ICY `StreamTitle` Extraktion, Trennung Artist/Title, Trennzeichenlogik, Titel-Bereinigung, Artist-Varianten, Generik-Filter-Funktionen
- `musicbrainz.py`
  - Matching-Entscheidungen, Recording/Artist Lookups, Album-Auswahl, Song-Kontext-Aufloesung, Similarity
- `radiode.py`
  - Parsing des radio.de now-playing Titelformats, Now-Playing-Abfrage (Slug/Suche), Logo-Aufloesung
- `tunein.py`
  - Station-ID-Erkennung aus Plugin-URL, Parsing TuneIn-Titelformate (JSON/Text), Now-Playing-Abfrage
- `cache.py`
  - Thread-safe MB Song-Cache mit TTL
- `api_client.py`
  - HTTP-Client mit Retry + Exponential-Backoff
- `source_policy.py`
  - zustandsbasierte Trigger-Entscheidung ueber Quellenfamilien (`musicplayer`, `api`, `icy`)
- `station_profiles.py`
  - persistente Senderprofile je Station (EMA-Lernen, Confidence, Policy-Profilableitung)
- `song_db.py`
  - SQLite-Datenbank (`song_data.db`): Songs-LRU-Cache (max. 200 pro Sender) und Generic-Keywords (Jingles/Stationsinfos) je Sender
- `constants.py`
  - Endpunkte, Header, Timeouts, Property-Namen, Regex-Konstanten
- `logger.py`
  - zentrales Logging-Praefix

## 3) Laufzeitarchitektur (High-Level)

Startpunkt:
- `service.py` -> `if __name__ == '__main__': RadioMonitor().run()`

Event-/Polling-Quellen:
- `PlayerMonitor` (Kodi `xbmc.Player`):
  - `onPlayBackStarted()`: erkennt API-Source frueh (radio.de/radio.de light/TuneIn)
  - `onPlayBackStopped()` / `onPlayBackEnded()`: loeschen Labels sofort (ohne Polling-Verzoegerung)
  - `onAVStarted()`: behandelt Video/Lokaldateien als harte Stop-Szenarien und liest frueh Logo aus `ListItem.Icon`
- `RadioMonitor.run()`:
  - Polling alle 2 Sekunden (`check_playing()`)

Worker:
- `metadata_worker(url, generation)` fuer ICY-Streams
- `api_metadata_worker(generation)` als Fallback bei fehlendem ICY (nur whitelisted API-Source)
- `_musicplayer_metadata_fallback(generation)` als Fallback ohne ICY und ohne API-Basis

## 4) Zustandsmodell und Thread-Sicherheit

Wichtige Runtime-Felder in `RadioMonitor`:
- `is_playing`, `current_url`
- `metadata_thread`, `stop_thread`
- `metadata_generation` (stale worker invalidation)
- `api_source`, `use_api_fallback`, `station_slug`, `plugin_slug`, `tunein_station_id`, `station_logo`
- `_last_song_time`, `_song_timeout` (song timeout management)
- `source_policy`, `_last_policy_context`, `_policy_preferred_source`
- `_profile_store`, `_station_profile_session`, `_active_policy_profile`, `_station_profile_policy_enabled`
- `_session_icy_song_seen`, `_session_api_stable_pair`, `_session_api_stable_polls` (API-only Startup-Heuristik)

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
  - setzt nur vorlaeufig `RadioMonitor.Title` (bewusst noch kein Artist-Trigger)
  - setzt `RadioMonitor.Playing=true`
  - startet `start_metadata_monitoring(playing_file)`

### 5.2 Stop-/Wechsel-Szenarien

Bei Video, lokaler Datei, Stream-Ende oder Addon-Ende:
- `stop_metadata_monitoring()`
- `clear_properties()`

Bei Streamwechsel:
- `_handle_stream_transition(...)` leert alte Labels vor dem Neubefuellen.
- Aufgerufen sowohl im `onPlayBackStarted()`-Pfad (frueh) als auch als Safety-Net im Polling (`check_playing()`).

`clear_properties()` loescht:
- radio.de Window-Properties (`RadioDE.StationLogo`, `RadioDE.StationName`)
- alle `RadioMonitor.*` Properties inkl. Timer-/Debug-Properties
- interne API/Logo/Timeout-Zwischenwerte

## 6) Metadaten-Pipeline und Fallback-Kette

### 6.1 ICY-Pfad (primaer)

`metadata_worker()` -> `parse_icy_metadata(url)`:
- Request mit Header `Icy-MetaData: 1`
- liest `icy-name`, `icy-genre`, `icy-metaint`
- schreibt Station/Genre-Properties erst nach stabilem Start (Buffering-Gate), nicht beim Header-Read
- wenn `icy-metaint` fehlt:
  - `_setup_api_fallback_from_url(url)`
  - kein ICY-Worker, stattdessen Fallback-Worker

Im Loop:
- liest Audio-Bytes bis `metaint`, dann Metadatenblocklaenge und Metadatenblock
- extrahiert `StreamTitle` via `metadata.extract_stream_title`
- API-Refresh nur nach stabilem Start oder nach gesetzter Erstquelle (kein Vorbefuellen waehrend Buffering)
- bei Titelwechsel:
  - invalidiert MB Song-Cache: `_mb_cache.clear()`
  - parst Artist/Title via `parse_stream_title(stream_title, station_name, url)`
- aktualisiert periodisch `RadioMonitor.ApiNowPlaying` aus der aktiven API-Quelle (throttled)

### 6.2 parse_stream_title() und Source-Policy

1. Kandidatenbildung (MusicPlayer + API + ICY)
- MusicPlayer-Kandidaten (direkt + swapped)
- API-Kandidat nur wenn Source whitelisted ist und ein valider API-Titel vorliegt
- ICY-Kandidaten aus `metadata.parse_stream_title_complex()` (direkt + optional swapped)

2. MB-Winner
- jeder Kandidat wird per MB bewertet (`score`, `artist_sim`, `title_sim`, `combined`)
- Winner nur oberhalb der Schwellwerte (`MB_WINNER_MIN_SCORE=60`, `MB_WINNER_MIN_COMBINED=55.0`)
- Tie-Break bei Gleichstand: ICY wird bevorzugt
- MB-Bereinigung der Schreibweise: Winner liefert `corrected_artist`/`corrected_title` – nur wenn `artist_sim >= MB_LABEL_CORRECTION_MIN_SIM` (0.85) UND `title_sim >= MB_LABEL_CORRECTION_MIN_SIM`; sonst bleiben Originalwerte (`input_artist`/`input_title`) fuer die Labels massgeblich; interne Quellentracking-Werte verwenden immer die Originalwerte

3. Sonderfall alle MB-Scores = 0
- bei aktivem Source-Lock bleibt die gelockte Quelle massgeblich (kein API-Override gegen Lock)
- wenn MusicPlayer (direkt/swapped) konsistent zu API oder ICY ist: MusicPlayer wird uebernommen
- wenn API-Kandidat gegenueber letzter API-Antwort gewechselt hat: API wird uebernommen
- wenn kein valider ICY-Kandidat existiert (z.B. numerische ICY-IDs): API wird ebenfalls uebernommen
- sonst: keine belastbaren Songdaten -> Rueckgabe `Artist=None`, `Title=None`

4. ICY-Analyse/Fallback
- `metadata.parse_stream_title_complex()`
- erkennt `"Title" von Artist`, mehrere Separatoren, Station-/Invalid-Werte
- numerische Formate wie `123 - 456` gelten als "kein ICY"
- Mehrfach-Separator-Heuristik:
- bei mehrfach ` - ` zuerst last-separator-Variante pruefen

5. MB-Entscheidung fuer ICY-Fallback
- `identify_artist_title_via_musicbrainz(...)`
- bei `uncertain=True` bleiben Eingabewerte konservativ erhalten, unsichere MB-Felder werden geleert
- MB-Bereinigung: wenn `artist_sim >= MB_LABEL_CORRECTION_MIN_SIM` UND `title_sim >= MB_LABEL_CORRECTION_MIN_SIM`, werden `display_artist`/`display_title` auf MB-Werte korrigiert; Quellentracking (`_set_last_song_decision`) nutzt unveraendert die Originalwerte

6. Trigger-Entscheidung
- `_determine_source_change_trigger(...)` delegiert die Trigger-Entscheidung an `SourcePolicy.decide_trigger(...)`.
- Policy-Scoring basiert auf Validitaet, Generic-Anteil, Churn, Uebereinstimmung mit letzter Gewinnerquelle und Lead-Errors.
- API-Konflikte werden nur noch gegen verlaessliche Vergleichsquellen geprueft:
  - Vergleich via MusicPlayer nur wenn nicht `mp_absent`/`mp_noise`
  - Vergleich via ICY nur wenn nicht `icy_structural_generic`
- Bei gesetzter Gewinnerquelle bleiben Trigger konservativ; Quelle wechselt nur bei bestaetigten, plausiblen Signalen.

### 6.3 API-Fallback-Worker

`api_metadata_worker()` (Intervall 10s):
- aktiviert nur wenn API-Source whitelisted ist und API-Basis vorhanden ist
- holt Titel via `get_nowplaying_from_apis()`
- bei Artist+Title:
  - MB-Validierung/Anreicherung
  - MB-Felder nur behalten wenn MB zum API-Titel plausibel passt
  - MB-Bereinigung: `display_artist`/`display_title` werden auf MB-Werte korrigiert wenn `artist_sim >= MB_LABEL_CORRECTION_MIN_SIM` UND `title_sim >= MB_LABEL_CORRECTION_MIN_SIM`; Titelwechsel-Erkennung verwendet stets den originalen API-Wert
- bei nur Title:
  - Artist/MBID werden bewusst nicht aggressiv geloescht (stabileres AS-Verhalten)
- `RadioMonitor.ApiNowPlaying` zeigt nur echte API-Titel (radio.de/TuneIn), nicht MusicPlayer-Fallback

### 6.4 MusicPlayer-Fallback-Worker

`_musicplayer_metadata_fallback()` (Intervall 5s):
- fuer Streams ohne ICY und ohne API-Basis
- pollt `MusicInfoTag` Artist/Title auf Aenderung
- bei Aenderung:
  - MB-Recording-Query fuer Normalisierung + MB-Felder
  - MB-Bereinigung: wenn `artist_sim >= MB_LABEL_CORRECTION_MIN_SIM` UND `title_sim >= MB_LABEL_CORRECTION_MIN_SIM`, werden die Labels auf MB-Werte korrigiert; andernfalls werden die MusicPlayer-Originalwerte beibehalten
  - aktualisiert Logo optional aus `Player.Icon` (z.B. AzuraCast per-song Cover)
- wenn keine verwertbaren Artist/Title-Daten mehr vorhanden: deaktiviert `Playing`

### 6.5 Senderprofile und adaptive Policy

`StationProfileStore` sammelt pro Station Session-Metriken und speichert sie in `profile_store/*.json`.

Session-Metriken (Auszug):
- Winner-Shares je Quellenfamilie
- ICY-Generic-Rate
- API-Verfuegbarkeit und API-Lag (in Poll-Zyklen)
- MP-Zuverlaessigkeit, MP-Song-Rate, MP-Flip-Rate

Ableitungen aus EMA-Profilen:
- `icy_structural_generic`: ICY ist strukturell meist generisch
- `mp_absent`: MusicPlayer liefert quasi nie Songdaten
- `mp_noise`: MusicPlayer schwankt stark und ist dabei unzuverlaessig

Policy-Integration:
- Beim Start einer Stations-Session wird eine Profil-Session geoeffnet.
- Das Policy-Profil wird erst aktiviert, wenn Startup stabil ist und verwertbare Daten vorliegen (ICY-Song oder API-only-Freigabe).
- Bei Session-Ende werden Metriken in das Profil zurueckgeschrieben (Confidence/Felder aktualisiert).

API-only Startup-Heuristik:
- Falls ICY/MP initial nur generisch/leer sind, kann der "Initialer Song-Block" aufgehoben werden.
- Voraussetzungen:
  - API liefert ein nicht-generisches Song-Paar
  - API-Paar ist ueber mindestens `STARTUP_API_ONLY_STABLE_POLLS` (aktuell `3`) Polls stabil
  - in der Session wurde noch kein valider ICY-Song gesehen
- Alternativ kann ein bestehendes Stationsprofil den API-only-Fall direkt freigeben (`confidence >= 0.20` plus Rollen-Flags).

### 6.6 SQLite-Datenbank (`song_data.db`)

`SongDB` verwaltet zwei Tabellen:

**`songs`** — bestaetigte Songs als LRU-Cache pro Sender:
- Spalten: `station_key`, `artist`, `title`, `mb_data` (JSON), `last_seen`
- Max. `SONG_CACHE_MAX_PER_STATION` (200) Eintraege je Sender; aelteste werden bei Ueberschreitung verdraengt
- Wird beim Stationswechsel konsultiert, um zuvor erkannte Songs schneller zu verarbeiten
- Schreibzugriff: UPSERT bei jedem neuen Song-Winner

**`generic_strings`** — sender-spezifische Jingle-/Stationsinfo-Strings:
- Spalten: `station_key`, `string`, `seen`, `last_seen`, `promoted`
- Wird NUR befuellt, wenn kein Song aktiv ist und kein Song in der Session bestaetigt wurde
- Kandidaten: ICY-StreamTitle und API-Titel (normalisiert, Mindestlaenge `GENERIC_STRING_MIN_LEN=8`)
- Strings mit langen Ziffernfolgen (`> GENERIC_STRING_MAX_DIGIT_SEQ=3`) werden verworfen
- Promotion: nach `KEYWORD_PROMOTE_MIN_SEEN=5` Beobachtungen wird `promoted=1` gesetzt
- Promotete Strings werden als Filter-Keywords verwendet, um nicht-songartige ICY-Bloecke zu erkennen

Migration:
- `_migrate()` erkennt altes Schema (Spalten `seen_generic`/`seen_song`) und baut die Tabelle neu auf

## 7) Property-Contract (kritisch)

Die Property-Reihenfolge ist absichtlich.

Wichtig:
- `RadioMonitor.Artist` wirkt als Trigger fuer Artist Slideshow
- deshalb muss `RadioMonitor.ArtistMBID` vorher gesetzt sein
- initial bei Streamstart wird Artist bewusst noch nicht gesetzt

Typische Setz-Reihenfolge im Metadatenpfad:
1. `Title`
2. `Album`
3. `AlbumDate`
4. `ArtistMBID`
5. `FirstRelease`
6. `Artist` (Trigger)
7. `Logo`
8. optional spaeter `BandFormed`, `BandMembers`, `Genre`

Bei klar fehlenden Songdaten werden song-bezogene Felder geloescht:
- `Artist`, `Title`, `Album`, `AlbumDate`, `ArtistMBID`, `FirstRelease`, `BandFormed`, `BandMembers`, `Genre`
- `Station` und `StreamTitle` bleiben fuer die Anzeige erhalten
- `ApiNowPlaying` wird separat aus der API-Refresh-Logik gepflegt

## 8) Song-Timeout

Zentrale Methoden:
- `_compute_song_timeout(duration_ms)`
- `_start_song_timeout(duration_ms)`
- `_update_timeout_remaining_property()`
- `_reset_song_timeout_state(clear_debug=...)`

Regeln:
- nach gueltigem Titelupdate startet der Timer neu
- wenn MB-Laenge bekannt: `timeout = max(0, duration_ms/1000 - SONG_TIMEOUT_EARLY_CLEAR_S)`
- wenn MB-Laenge nicht bekannt: Fallback `240s`

Wenn Timeout ablaeuft:
- song-bezogene Properties werden geloescht
- Timer-Debug-Properties werden zurueckgesetzt

Timer-Debug-Properties:
- `RadioMonitor.MBDurationMs`
- `RadioMonitor.MBDurationS`
- `RadioMonitor.TimeoutTotal`
- `RadioMonitor.TimeoutRemaining`

## 9) MusicBrainz-Logik (Kernpunkte)

### 9.1 Query-Strategie Artist/Title-Reihenfolge

`identify_artist_title_via_musicbrainz(part1, part2)`:
- Q1: `recording:part1 AND artistname:part2`
- Q2: `recording:part2 AND artistname:part1`
- Q3 Fallback (nur Titel) wenn Q1+Q2 keine Treffer liefern
- Entscheidung ueber kombinierten Wert: `MB score * artist similarity`
- Schwellwerte Kandidatenauswahl:
  - `MB_WINNER_MIN_SCORE = 60`
  - `MB_WINNER_MIN_COMBINED = 55.0`
- Schwellwert Label-Bereinigung:
  - `MB_LABEL_CORRECTION_MIN_SIM = 0.85` (gilt fuer artist_sim UND title_sim gleichzeitig)
  - nur wenn beide Aehnlichkeiten >= 0.85: Labels werden auf MB-Schreibweise korrigiert
  - sonst: Originalwerte aus der Quelle bleiben als Labels erhalten

### 9.2 Album-/FirstRelease-Auswahl

`musicbrainz_resolve_song_context(...)`:
- nutzt Work-Relations des gewaehlten Recordings
- sammelt passende Work-Recordings (Paging begrenzt)
- filtert auf erwarteten Artist
- bestimmt Song-FirstRelease ueber passende Recordings
- zieht Releases nach und waehlt fruehes passendes Album

### 9.3 Caches

- `_mb_cache`: Song-Cache (TTL aus `MB_SONG_CACHE_TTL`, default 24h)
- `_artist_info_cache`: In-Memory Cache fuer Artist-Infos
- bei StreamTitle-Aenderung im ICY-Worker: `_mb_cache.clear()` zur Vermeidung stale MBIDs

## 10) Invarianten fuer Refactoring

Nicht verletzen ohne expliziten Grund:
- Property-Setzreihenfolge (insb. ArtistMBID vor Artist)
- Source-Policy-Entscheidung nach Erstentscheidung (Quellenwechsel nur bei bestaetigten, plausiblen Signalen)
- getrennte, aehnlich aussehende Property-Bloecke nicht blind zusammenfuehren
- numerische ICY-Titel als "kein ICY" behandeln
- MB-Cache bei StreamTitle-Wechsel invalidieren
- API nur fuer whitelisted Quellen nutzen
- API-only Startup-Bypass nur unter den vorgesehenen Heuristik-/Profilbedingungen aktivieren
- MB-Bereinigung nur wenn beide Aehnlichkeitsschwellen erfuellt (`MB_LABEL_CORRECTION_MIN_SIM`); MB darf Labels niemals komplett ersetzen wenn der Treffer ein anderer Song ist
- Quellentracking (`_set_last_song_decision`) immer mit Originalwerten aus der Quelle – nie mit MB-korrigierten Werten

## 11) Debugging-Playbook

1. Kodi Debug-Logging aktivieren.
2. In `kodi.log` nach `[Audio Stream Monitor]` filtern.
3. Erwartete Kernmarker:
   - `STREAM GESTARTET - INITIAL STATE`
   - `ICY METADATA (ROH)`
   - `Neuer StreamTitle erkannt`
   - `MB-Cache invalidiert wegen Titelwechsel`
   - `MB score=0 fuer alle Kandidaten, keine belastbaren Songdaten -> nutze nur Station/StreamTitle`
   - `MusicBrainz Entscheidung`
   - `Song-Timeout abgelaufen`

Skin-Debug-Anzeige:
```xml
<label>MB: $INFO[Window(Home).Property(RadioMonitor.MBDurationS)]s</label>
<label>Timer: $INFO[Window(Home).Property(RadioMonitor.TimeoutRemaining)] / $INFO[Window(Home).Property(RadioMonitor.TimeoutTotal)]s</label>
```

## 12) Erweiterungspunkte

Sichere Erweiterungen:
- neue API-Quelle als eigenes Modul (analog `radiode.py`/`tunein.py`) anlegen und in `get_nowplaying_from_apis()` integrieren
- neue Normalisierungen in `metadata.py` ergaenzen
- zusaetzliche MB-Heuristiken in `musicbrainz.py` hinter bestehende Schwellenwerte legen

Vorher pruefen:
- beeinflusst die Aenderung Artist/ArtistMBID Trigger-Reihenfolge?
- kann sie stale Properties bei Streamwechsel erzeugen?
- bleibt Fallback-Kette (ICY -> API -> MusicPlayer) konsistent?
