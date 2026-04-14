# AGENTS.md

## Mission und Scope
- Dieses Repo ist ein Kodi-Service-Addon (`addon.xml`, `<extension point="xbmc.service" library="service.py"/>`), kein CLI/Server-Projekt.
- Ziel ist stabile `RadioMonitor.*` Window-Properties fuer Skins/Addons bei HTTP/HTTPS-Audio-Streams.
- API-Now-Playing ist absichtlich auf whitelisted Quellen begrenzt (`plugin.audio.radiode`, `plugin.audio.radio_de_light`, `plugin.audio.tunein2017`; siehe `constants.py`).

## Architektur (zuerst lesen)
- `service.py`: Orchestrator (`RadioMonitor`, `PlayerMonitor`), Worker-Start/Stop, Fallback-Kette, Property-Setzen/Clearing.
- `metadata.py`: ICY-Parsing (`StreamTitle`), Separator-/"von"-Logik, Generic-Filter.
- `source_policy.py` + `station_profiles.py`: zustandsbasierte Quellenwahl + lernende Stationsprofile (EMA).
- `song_db.py`: SQLite fuer bestaetigte Songs, Generic-Keywords, `verified_station_sources` Shared-Contract.
- `musicbrainz.py`: MB-Matching/Anreicherung; nutzt harte Schwellen statt aggressiver Umbenennung.

## Kritische Laufzeit-Invarianten
- Property-Reihenfolge nicht brechen: `ArtistMBID` vor `Artist` (AS-Trigger), Details in `TECHNISCHE_DOKUMENTATION.md`.
- Bei Streamwechsel/Stop immer erst alte Labels loeschen (`_handle_stream_transition`, `clear_properties`).
- MB-Korrektur nur bei hoher Aehnlichkeit (`MB_LABEL_CORRECTION_MIN_SIM`), sonst Quellwerte behalten.
- Numerische ICY-Paare (z. B. `123 - 456`) als Nicht-Song behandeln (`NUMERIC_ID_PATTERN`).
- `self._persist_data` gate't DB/Profile-Schreibzugriffe (u. a. `song_data.db`, `station_profiles`); Analyse-Events (`analysis_events.jsonl`) sind davon nicht betroffen.
- ASM-QF `no_hit` ist im autoritativen QF-Zustand kurz gepuffert (`QF_NO_HIT_HOLD_S` in `service.py`): transientes `no_hit` darf Artist/Title nicht sofort leeren.
- QF-Diagnose-Logs laufen zentral ueber `ASM-QF DIAG event=...` (key=value); neue QF-Logs nicht als freie Textlogs duplizieren.
- QF-Request/Response-Vertrag einhalten: jede `RadioMonitor.QF.Request.Id` braucht genau eine terminale `RadioMonitor.QF.Response.Id` (auch `superseded`/`cancelled`/`error`/`no_hit`). Kein stilles Verwerfen.

## Datenfluss und Fallbacks (entscheidend)
- Primaer ICY (`metadata_worker`) -> MB-Winner + `SourcePolicy.decide_trigger(...)`.
- Wenn kein `icy-metaint`: API-Fallback nur fuer whitelisted Quellen (`api_metadata_worker`).
- Wenn weder ICY noch API-Basis: MusicPlayer-Fallback (`_musicplayer_metadata_fallback`).
- Optionaler externer QF-Pfad: Request/Response via Window-Properties `RadioMonitor.QF.*`; QF kann im Lock autoritativ sein.
- Supersede-Regel: wird ein laufender QF-Request intern ueberholt, muss die alte Request-ID explizit mit terminalem Status beantwortet werden, sonst bleibt ASM bis `QF_NO_RESPONSE_FALLBACK_S` im no-response-Wartefenster.
- Aktiver ASM-QF-Lock: reiner ICY-`StreamTitle`-Wechsel triggert keinen Quellenwechsel; QF-Paarwechsel darf im Lock auch ohne strikten `fresh`-Request-ID-Match erkannt werden.
- QF-Prefill fuer Skin-Kompatibilitaet: bei QF-`hit` werden `RadioMonitor.Artist` und `RadioMonitor.ArtistDisplay` synchron gesetzt.
- Bei aktivem QF-no-hit-hold werden Trigger/Clears defensiv geparkt (`hold_park_trigger`, `hold_skip_no_usable_clear`), Song-Ende-Signale (Detektor/Timeout) bleiben davon unberuehrt.

## Integrationen und Vertraege
- TuneIn: nur `Describe.ashx` mit `partnerId=HyzqumNX` (`tunein.py`), `has_song=False` ist harter No-Song-Exit.
- radio.de: Slug/Details/Now-Playing ueber `radiode.py`; bei bekanntem Slug kein unsicherer Such-Fallback.
- Shared DB Vertrag mit ASM-QF: `docs/00_ueberblick/ASM_QF_SHARED_DB_CONTRACT.md` (`verified_station_sources`).
- Song-Historie UI-Action: `RunScript(.../default.py,show_song_history)` aus `resources/settings.xml`.

## Dev-Workflow (repo-spezifisch)
- Es gibt keine lokale Test-Suite/Build-Pipeline im Repo; Validierung erfolgt zur Laufzeit in Kodi.
- Haupt-Debugquelle ist `kodi.log`, filter auf `[Audio Stream Monitor]` (Marker in `TECHNISCHE_DOKUMENTATION.md`, Abschnitt Debugging-Playbook).
- Fuer QF-Timing/Freshness zusaetzlich auf `ASM-QF DIAG` filtern (`event=non_fresh`, `fresh_reason`, `gap_source`, `gap_s`, `event=hold_*`).
- Bei Timing-/Flapping-Problemen zuerst Konstanten in `constants.py` anpassen (`PLAYER_BUFFER_*`, `API_*_INTERVAL_S`, `SONG_TIMEOUT_*`, `SONG_END_*`).
- Nach Aenderungen an Settings/Properties immer prüfen: Skin-Kompatibilitaet (`Artist` vs `ArtistDisplay`) und stale-label Verhalten bei Stop/URL-Wechsel.

## Aenderungs-Checkliste fuer Agents
- Betroffene Quelle klar benennen (`asm-qf`/`musicplayer`/`api`/`icy`) und Triggergrund dokumentieren.
- Sicherstellen, dass `source_policy` + Stationsprofil-Hints zusammenpassen (keine isolated Logik in `service.py` duplizieren).
- Bei DB-Aenderungen Migration in `SongDatabase._migrate()` mitdenken.
- Keine neuen API-Calls fuer nicht-whitelisted Quellen einbauen.
- Bei neuen Properties: in `constants.py` (`PropertyNames`) aufnehmen und in `clear_properties()` konsequent loeschen.

