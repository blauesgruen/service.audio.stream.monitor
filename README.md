# Audio Stream Monitor

Dieses Service-Addon überwacht **alle HTTP/HTTPS Audio-Streams** und liest die Metadaten (Titel, Interpret, etc.) korrekt aus.

Das Monitoring funktioniert mit jedem Addon, das HTTP/HTTPS Audio-Streams abspielt.
**API-Now-Playing wird bewusst nur fuer folgende Quellen genutzt:** `plugin.audio.radiode`, `plugin.audio.radio_de_light`, `plugin.audio.tunein2017`.

## Features

- ✅ Universelle Unterstützung für alle HTTP/HTTPS Audio-Streams
- ✅ Automatische Erkennung von Radio- und Musik-Streams
- ✅ Auslesen von ICY-Metadaten (Icecast/Shoutcast)
- ✅ Intelligente Trennung von Artist und Title (mehrere Trennzeichen, 'von'-Format, last-separator-Variante)
- ✅ Defensiver Umgang mit numerischen ICY-Formaten: `digit-digit`-Strings wie `123 - 456` gelten als Nicht-Song; bei `MB score=0` mit numerischen Teilstrings wird API bevorzugt bzw. kein Song gesetzt
- ✅ Stationsname-Filterung: ICY-Strings, die nur den Sendernamen enthalten, werden nicht als Artist/Title übernommen
- ✅ MusicBrainz-Abgleich zur Kandidatenauswahl und Datenanreicherung (Album, AlbumDate, FirstRelease, MBID, Genre, Banddaten)
- ✅ MB-Bereinigung der Schreibweise: Artist/Title werden korrigiert wenn MB eindeutig denselben Song bestätigt (`MB_LABEL_CORRECTION_MIN_SIM=0.85`); bei komplett anderem MB-Treffer bleiben Originalwerte erhalten
- ✅ Konservative MB-Kandidatenauswahl über kombinierten Score (`MB_WINNER_MIN_SCORE=60`, `MB_WINNER_MIN_COMBINED=55.0`)
- ✅ Erweitertes Artist-Matching: CamelCase-Splitting ("DeBurgh" → "De Burgh"), Komma-Umkehr, Apostroph-Normalisierung und tokenbasierter Fallback
- ✅ Intelligente Album-Auswahl: nur Releases des gewählten Best-Recordings werden berücksichtigt; bevorzugt wird das erste passende Studioalbum (Special-/Exclusive-Editionen werden bei Gleichstand nachrangig behandelt)
- ✅ Klammern-Bereinigung im Titel vor MB-Suche: Metadaten-Tags wie "(Radio Edit)" oder "(Remastered 2011)" werden iterativ entfernt, inhaltliche Klammern wie "(Love theme)" bleiben erhalten
- ✅ radio.de- und TuneIn-Now-Playing API als priorisierte Metadaten-Quelle (vor ICY), jedoch nur fuer whitelisted Addons; TuneIn-Abfrage ueber `Describe.ashx` mit Partner-ID (korrekte `has_song`/Now-Playing-Antworten)
- ✅ Source-Policy nach Erstentscheidung: Songwechsel werden ueber eine zustandsbehaftete Quellen-Policy (`musicplayer`/`api`/`icy`) bewertet; Wechsel erfolgen nur bei belastbaren Signalen; ICY-Songwechsel werden sofort erkannt (kein Multi-Poll-Confirm, da ICY-Metadaten nur einmal pro Songwechsel ankommen)
- ✅ Wenn MB-Scores aller Kandidaten = 0, bleibt bei aktivem Source-Lock die gelockte Quelle fuer Artist/Title massgeblich
- ✅ MusicPlayer wird als Songquelle mitbewertet (direkt + swapped) und kann bei MB-Nulltreffern ueber Konsens mit API/ICY uebernommen werden
- ✅ Lernende Senderprofile pro Station (persistiert als JSON): Confidence, dominante Quellenfamilie, API-Lag und adaptive Policy-Gewichte
- ✅ SQLite-Datenbank: bestätigte Songs als LRU-Cache (bis 200 pro Sender), Tageszaehler und sender-spezifische Generic-Keywords (Jingles/Stationsinfos) zur Filterung nicht-songartiger ICY-Blöcke
- ✅ Song-DB-Persistenz nur fuer MB-verifizierte Songs (MBID erforderlich)
- ✅ Recount-Schutz: gleicher Song pro Sender wird innerhalb von `10` Minuten nicht erneut gezaehlt (`SONG_RECOUNT_WINDOW_S`)
- ✅ Struktur-Flags aus Senderprofilen verbessern Quellbewertung: `icy_structural_generic`, `mp_absent`, `mp_noise`
- ✅ Startup-Bypass fuer API-only-Sender: initialer Song-Block wird aufgehoben, wenn API stabil liefert und ICY/MusicPlayer keine belastbaren Songs liefern
- ✅ `RadioMonitor.ApiNowPlaying` wird periodisch aktualisiert (auch ohne StreamTitle-Wechsel)
- ✅ API/ICY-Property-Befuellung erst nach stabilem Start (Kodi-Buffering vorbei), Logo weiterhin sofort
- ✅ Station-ID direkt aus Logo-URL: kein Fehlmatching mehr bei abweichenden ICY-Namen (z.B. NRJ CLUBBIN → ENERGY Clubbin')
- ✅ Stationsname via radio.de Details-API wenn Station-ID aus Logo bekannt
- ✅ MusicPlayer-Fallback fuer Streams ohne ICY und ohne verfuegbare API-Basis (z.B. AzuraCast, Ampache): erkennt Titelwechsel bei Live-Streams, verarbeitet Metadaten via MusicBrainz
- ✅ Logo-Update bei Titelwechsel: AzuraCast-Streams liefern pro Song ein anderes Album-Cover
- ✅ Song-Timeout: Properties werden automatisch gelöscht wenn der Song abgelaufen ist (MB-Songlänge minus `SONG_TIMEOUT_EARLY_CLEAR_S`; wenn keine MB-Songlänge vorliegt: Fallback 4 min) – verhindert veraltete Metadaten bei Sendern ohne Titelwechsel-Signal
- ✅ Debug-Properties für Timeout-Validierung: MB-Songdauer und Live-Countdown als Window-Properties sichtbar
- ✅ Sofortiges Löschen der Labels bei Stop/Ende (Player-Callbacks)
- ✅ Streamwechsel wird abgefangen: Labels werden vor Neubefuellung zuerst geloescht
- ✅ Addon-Settings: Bullet-Punkt (an/aus + Farbe aus Skin-Farbpalette) und optionale Deaktivierung der DB/JSON-Persistenz
- ✅ ICY-Rohdaten-Fallback: bei MB-Score=0 und fehlendem API werden Artist/Title direkt aus dem ICY-Split übernommen (z.B. DJ-Sets, Radiosendungen)

## Verfügbare Window Properties

Das Service-Addon setzt folgende Properties, die in der Kodi-Skin verwendet werden können:

| Property                        | Beschreibung | Beispiel |
|---------------------------------|--------------|----------|
| `RadioMonitor.Playing`          | "true" wenn Radio läuft | true |
| `RadioMonitor.Station`          | Name des Radiosenders | "Bayern 3" |
| `RadioMonitor.Title`            | Aktueller Song-Titel | "Bohemian Rhapsody" |
| `RadioMonitor.ArtistDisplay`    | Aktueller Interpret – mit konfiguriertem Bullet-Präfix (für Skin-Anzeige) | "• Queen" |
| `RadioMonitor.ArtistMBID`       | MusicBrainz Artist-ID | "0383dadf-2a4e-4d10-a46a-e9e041da8eb3" |
| `RadioMonitor.Album`            | Album (via MusicBrainz) | "A Night at the Opera" |
| `RadioMonitor.AlbumDate`        | Erscheinungsjahr des Albums | "1975" |
| `RadioMonitor.FirstRelease`     | Jahr der Erstveröffentlichung des Songs | "1975" |
| `RadioMonitor.StreamTitle`      | Vollständiger StreamTitle (roh) | "Queen - Bohemian Rhapsody" |
| `RadioMonitor.Source`           | Aktive Song-Quellenfamilie (`musicplayer`, `api`, `icy`) | "musicplayer" |
| `RadioMonitor.Genre`            | Genre des Künstlers (via MusicBrainz) | "alternative rock" |
| `RadioMonitor.Logo`             | URL zum Senderlogo | "https://cdn.radio.de/images/broadcasts/..." |
| `RadioMonitor.BandFormed`       | Gründungsjahr (nur bei Bands) | "1995" |
| `RadioMonitor.BandMembers`      | Aktuelle Mitglieder (nur bei Bands) | "Chad Kroeger, Mike Kroeger, Ryan Peake, Daniel Adair" |
| `RadioMonitor.MBDurationMs`     | Von MusicBrainz ermittelte Songdauer in Millisekunden | "175000" |
| `RadioMonitor.MBDurationS`      | Von MusicBrainz ermittelte Songdauer in Sekunden | "175" |
| `RadioMonitor.TimeoutTotal`     | Aktueller gesetzter Timeout in Sekunden | "160" |
| `RadioMonitor.TimeoutRemaining` | Verbleibende Zeit bis zum Label-Clear in Sekunden | "142" |

## Verwendung in Skins

### Beispiel 1: Title anzeigen
```xml
<label>$INFO[Window(Home).Property(RadioMonitor.Title)]</label>
```

### Beispiel 2: Artist - Title
```xml
<label>$INFO[Window(Home).Property(RadioMonitor.Artist)] - $INFO[Window(Home).Property(RadioMonitor.Title)]</label>
```

### Beispiel 3: Nur wenn Radio läuft
```xml
<control type="label">
    <label>$INFO[Window(Home).Property(RadioMonitor.StreamTitle)]</label>
    <visible>String.IsEqual(Window(Home).Property(RadioMonitor.Playing),true)</visible>
</control>
```

### Beispiel 4: Sender-Name
```xml
<label>[B]$INFO[Window(Home).Property(RadioMonitor.Station)][/B]</label>
```

### Beispiel 5: MB-Dauer + Timer-Countdown (Debug)
```xml
<label>MB: $INFO[Window(Home).Property(RadioMonitor.MBDurationS)]s</label>
<label>Timer: $INFO[Window(Home).Property(RadioMonitor.TimeoutRemaining)] / $INFO[Window(Home).Property(RadioMonitor.TimeoutTotal)]s</label>
```

### Beispiel 6: Aktuelle API-Daten
```xml
<label>API: $INFO[Window(Home).Property(RadioMonitor.ApiNowPlaying)]</label>
```

## Installation

### Empfohlene Methode: Kodinerds Repository

1. Füge die Kodinerds Repo-Quelle in Kodi hinzu: `https://repo.kodinerds.net`
2. Installiere das "Kodinerds Addon Repo" aus der ZIP-Datei.
3. Gehe zu "Aus Repository installieren" → "Kodinerds Addon Repo" → "Dienste".
4. Wähle "Audio Stream Monitor" und installiere es.

### Manuelle Installation

1. Kopiere den Ordner `service.audio.stream.monitor` in dein Kodi Addon-Verzeichnis:
   - Windows: `%APPDATA%\Kodi\addons\`
   - Linux: `~/.kodi/addons/`
   - Portable: `<Kodi-Ordner>\portable_data\addons\`

2. Starte Kodi neu. Das Service wird automatisch aktiviert und läuft im Hintergrund.

## Support

- Support-Thread (Kodinerds): https://www.kodinerds.net/thread/80816-release-audio-stream-monitor-service-addon/

## Technische Details

### ICY-Metadata
Das Addon sendet den Header `Icy-MetaData: 1` beim Stream-Abruf und parst die Metadaten kontinuierlich aus dem Stream.

### Metadaten-Format
StreamTitle wird normalerweise im Format `Artist - Title` übertragen. Das Addon erkennt folgende Trennzeichen:
- ` - ` (Leerzeichen-Minus-Leerzeichen)
- ` – ` (EN-Dash)
- ` — ` (EM-Dash)
- ` | ` (Pipe)
- `: ` (Doppelpunkt)

Parsing-Regeln:
- Bei mehrfach vorkommendem ` - ` wird zunaechst die **last-separator-Variante** geprueft und ueber MB validiert.
- Numerische `digit-digit`-Formate (z. B. `240846 - 245425`) werden als **Nicht-Song** behandelt.
- Bei `MB score=0` und numerischen Teilstrings im ICY-Paar wird kein numerischer ICY-String als Song uebernommen.

### API-Whitelist und MB-Schwelle
- API-Now-Playing wird nur verwendet, wenn die Quelle aus einem whitelisted Addon stammt (`plugin.audio.radiode`, `plugin.audio.radio_de_light`, `plugin.audio.tunein2017`).
- Für alle anderen Quellen werden keine radio.de/TuneIn-API-Calls ausgeführt.
- Die Artist/Title-Entscheidung bleibt konservativ: MusicBrainz nutzt kombinierten Score (`MB score * artist similarity`) und akzeptiert Kandidaten erst oberhalb der Schwellen (`MB_WINNER_MIN_SCORE=60`, `MB_WINNER_MIN_COMBINED=55.0`).
- MB-Bereinigung der Schreibweise: Labels werden nur korrigiert, wenn MB denselben Song mit ausreichend hoher Aehnlichkeit bestaetigt (`MB_LABEL_CORRECTION_MIN_SIM=0.85`); bei abweichendem MB-Treffer bleiben Originalwerte erhalten.
- Spezieller No-Song-Fall (MB-Score=0): bei aktivem Source-Lock bleibt die gelockte Quelle massgeblich; wenn API gewechselt hat oder kein ICY vorhanden ist, wird die API übernommen; existiert ein valides ICY-Direktpaar und kein API, werden Artist/Title direkt aus dem ICY-Split übernommen (ICY-Rohdaten-Fallback); sonst bleiben Artist/Title leer.
- Song-Historie-DB: Ein Song wird nur persistiert, wenn MB-Verifikation vorliegt (`MBID` gesetzt). Wiederholungen desselben Songs innerhalb von `10` Minuten pro Sender erhoehen den Zaehler nicht erneut.

### Source-Policy und Senderprofile
- Quellenwechsel werden ueber `SourcePolicy` mit Zustandsfenster (Validitaet, Generic-Rate, Churn, Agreement, Lead-Errors) bewertet.
- Pro Station lernt das Addon ein Profil (`profile_store/*.json`) und uebergibt dieses als Policy-Profil an die Laufzeit.
- Bei ausreichend hoher Profil-Confidence werden `weights`, `switch_margin` und `single_confirm_polls` adaptiv gesetzt.
- Struktur-Flags werden aus EMA-Metriken abgeleitet:
  - `icy_structural_generic` bei hoher ICY-Generic-Rate (>= `0.90`)
  - `mp_absent` bei sehr niedriger MusicPlayer-Song-Rate (<= `0.05`)
  - `mp_noise` bei hoher MP-Zustandsfluktuation (Flip-Rate >= `0.30` bei niedriger MP-Zuverlaessigkeit <= `0.25`)
- Startup-Sonderfall API-only: Der initiale Generic-Programmblock wird aufgehoben, wenn API mindestens `3` stabile Polls dasselbe Song-Paar liefert und ICY/MP keine verwertbaren Song-Paare liefern.

### Modulstruktur

| Modul | Inhalt |
|-------|--------|
| `metadata.py` | Metadaten-Parsing & Normalisierung, ICY-Extraktion, Artist-Varianten, Generik-Filter |
| `service.py` | PlayerMonitor, RadioMonitor, Service-Steuerung, Property-Management |
| `musicbrainz.py` | Alle MusicBrainz-API-Funktionen, Album-Auswahl, Artist-Info |
| `radiode.py` | radio.de API: Titel-Parser und Now-Playing-Abfrage |
| `tunein.py` | TuneIn API: Station-ID-Erkennung, Titel-Parser und Now-Playing-Abfrage |
| `constants.py` | API-URLs, Property-Namen (_P), Timeouts, Regex |
| `source_policy.py` | Zustandsbasierte Quellenbewertung und Trigger-Entscheidung (`musicplayer`/`api`/`icy`) |
| `station_profiles.py` | Persistente Senderprofile (EMA-Lernen), Policy-Profilableitung und Rollenerkennung (`mp_noise`, `mp_absent`, `icy_structural_generic`) |
| `song_db.py` | SQLite-Datenbank: MB-verifizierte Songs (LRU + Tageszaehler + 10-Minuten-Recount-Schutz) und sender-spezifische Generic-Keywords |
| `skin_colors.py` | Liest Farbdefinitionen des aktiven Skins und aktualisiert das Farb-Dropdown in `resources/settings.xml` (in-place) |
| `logger.py` | Logging-Wrapper (log_debug/info/warning/error) |
| `cache.py` | Thread-safe MusicBrainz-Cache mit TTL |
| `api_client.py` | HTTP-Client mit Retry und Exponential-Backoff |

### Performance
- Überprüft alle 2 Sekunden, ob ein Stream läuft
- Metadaten-Parsing läuft in separatem Thread
- Minimaler CPU-/Speicher-Verbrauch

## Debugging

Das Addon schreibt wichtige Ereignisse (z.B. Songwechsel) standardmäßig in die `kodi.log`. Für eine detaillierte Analyse, insbesondere bei Problemen mit der Titelerkennung, sollte das Debug-Logging in Kodi aktiviert werden.

1. **Log-Datei ansehen:** Die `kodi.log` befindet sich typischerweise unter:
   - Windows (normal): `%APPDATA%\Kodi\temp\kodi.log`
   - Windows (portable): `<Kodi-Ordner>\portable_data\temp\kodi.log`
   - Linux: `~/.kodi/temp/kodi.log`
2. **Suche nach:** `[Audio Stream Monitor]`
3. **Für detaillierte Logs:** Aktiviere "Debug-Logging" in Kodi unter `Einstellungen → System → Logging`.

## Kompatibilität

- Kodi 19 (Matrix) und höher
- Alle Plattformen (Windows, Linux, Android, etc.)
- Getestet mit: radio.de, radio.de light, TuneIn, Mother Earth Radio (AzuraCast), Intergalactic, I Love Music, Ampache

## Bekannte Limitierungen

- Nicht alle Radio-Streams senden ICY-Metadaten; für diese Streams greift entweder die API (nur whitelisted Addons: radio.de/radio.de light/TuneIn) oder der MusicPlayer-Fallback
- Manche Sender senden nur Sender-/Promo-Text statt Interpret/Titel. In solchen Faellen bleiben Artist/Title leer; Station und StreamTitle bleiben fuer die Anzeige erhalten.
- Einige TuneIn-Sender aktivieren kein Now-Playing (`has_song=False`); fuer diese Sender liefert die TuneIn-API keinen Songtitel – das Addon faellt auf ICY-Metadaten oder MusicPlayer zurueck.
- Bei verschlüsselten Streams (HTTPS) können manche Server keine ICY-Metadaten liefern
- Künstler ohne Einträge in fanart.tv oder theaudiodb liefern keine Hintergrundbilder für Artist Slideshow

## Lizenz

MIT License - siehe LICENSE.txt

## Danksagung

Dieses Addon wurde ursprünglich für das "Radio.de light" Addon von Publish3r entwickelt und zu einem universellen Service für alle Audio-Streams erweitert.
