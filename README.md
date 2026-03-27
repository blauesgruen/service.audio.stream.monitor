# Audio Stream Monitor

Dieses Service-Addon überwacht **alle HTTP/HTTPS Audio-Streams** und liest die Metadaten (Titel, Interpret, etc.) korrekt aus.

Das Monitoring funktioniert mit jedem Addon, das HTTP/HTTPS Audio-Streams abspielt.
**API-Now-Playing wird bewusst nur fuer folgende Quellen genutzt:** `plugin.audio.radiode`, `plugin.audio.radio_de_light`, `plugin.audio.tunein2017`.

## Features

- ✅ Universelle Unterstützung für alle HTTP/HTTPS Audio-Streams
- ✅ Automatische Erkennung von Radio- und Musik-Streams
- ✅ Auslesen von ICY-Metadaten (Icecast/Shoutcast)
- ✅ Intelligente Trennung von Artist und Title (mehrere Trennzeichen, 'von'-Format, last-separator-Variante)
- ✅ Stationsname-Filterung: ICY-Strings, die nur den Sendernamen enthalten, werden nicht als Artist/Title übernommen
- ✅ MusicBrainz-Abgleich zur Validierung und Korrektur von Artist, Title, Album, AlbumDate, FirstRelease und MBID
- ✅ Konservative MB-Entscheidung bei vertauschten Kandidaten über kombinierten Score (`MB score * artist similarity`) mit Schwellen (`MIN_SCORE=85`, `THRESHOLD=0.7`)
- ✅ Erweitertes Artist-Matching: CamelCase-Splitting ("DeBurgh" → "De Burgh"), Komma-Umkehr, Apostroph-Normalisierung und tokenbasierter Fallback
- ✅ Intelligente Album-Auswahl: nur Releases des gewählten Best-Recordings werden berücksichtigt; bevorzugt wird das erste passende Studioalbum (Special-/Exclusive-Editionen werden bei Gleichstand nachrangig behandelt)
- ✅ Klammern-Bereinigung im Titel vor MB-Suche: Metadaten-Tags wie "(Radio Edit)" oder "(Remastered 2011)" werden iterativ entfernt, inhaltliche Klammern wie "(Love theme)" bleiben erhalten
- ✅ radio.de- und TuneIn-Now-Playing API als priorisierte Metadaten-Quelle (vor ICY), jedoch nur fuer whitelisted Addons
- ✅ Wenn API/ICY keine belastbaren Songdaten liefern (z.B. MB-Scores aller Kandidaten = 0), bleiben `RadioMonitor.Artist` und `RadioMonitor.Title` leer; Ausnahme: API wird weiter genutzt, wenn sie gewechselt hat oder kein valider ICY-Kandidat vorliegt (z.B. numerische ICY-IDs)
- ✅ MusicPlayer wird als Songquelle mitbewertet (direkt + swapped) und kann bei MB-Nulltreffern ueber Konsens mit API/ICY uebernommen werden
- ✅ `RadioMonitor.ApiData` wird periodisch aktualisiert (auch ohne StreamTitle-Wechsel), damit API-Debug-Labels aktuell bleiben
- ✅ Station-ID direkt aus Logo-URL: kein Fehlmatching mehr bei abweichenden ICY-Namen (z.B. NRJ CLUBBIN → ENERGY Clubbin')
- ✅ Stationsname via radio.de Details-API wenn Station-ID aus Logo bekannt
- ✅ MusicPlayer-Fallback fuer Streams ohne ICY und ohne verfuegbare API-Basis (z.B. AzuraCast, Ampache): erkennt Titelwechsel bei Live-Streams, verarbeitet Metadaten via MusicBrainz
- ✅ Logo-Update bei Titelwechsel: AzuraCast-Streams liefern pro Song ein anderes Album-Cover
- ✅ Song-Timeout: Properties werden automatisch gelöscht wenn der Song abgelaufen ist (MB-Songlänge minus `SONG_TIMEOUT_EARLY_CLEAR_S`; wenn keine MB-Songlänge vorliegt: Fallback 4 min) – verhindert veraltete Metadaten bei Sendern ohne Titelwechsel-Signal
- ✅ Debug-Properties für Timeout-Validierung: MB-Songdauer und Live-Countdown als Window-Properties sichtbar
- ✅ Automatisches Löschen der Properties beim Stoppen
- ✅ Verhindert alte Metadaten beim Addon-Wechsel

## Verfügbare Window Properties

Das Service-Addon setzt folgende Properties, die in der Kodi-Skin verwendet werden können:

| Property | Beschreibung | Beispiel |
|----------|--------------|----------|
| `RadioMonitor.Playing` | "true" wenn Radio läuft | true |
| `RadioMonitor.Station` | Name des Radiosenders | "Bayern 3" |
| `RadioMonitor.Title` | Aktueller Song-Titel | "Bohemian Rhapsody" |
| `RadioMonitor.Artist` | Aktueller Interpret | "Queen" |
| `RadioMonitor.MBID` | MusicBrainz Artist-ID | "0383dadf-2a4e-4d10-a46a-e9e041da8eb3" |
| `RadioMonitor.Album` | Album (via MusicBrainz) | "A Night at the Opera" |
| `RadioMonitor.AlbumDate` | Erscheinungsjahr des Albums | "1975" |
| `RadioMonitor.FirstRelease` | Jahr der Erstveröffentlichung des Songs | "1975" |
| `RadioMonitor.StreamTitle` | Vollständiger StreamTitle (roh) | "Queen - Bohemian Rhapsody" |
| `RadioMonitor.ApiData` | Letzter valider API-Titel (radio.de/TuneIn) | "Artist - Title" |
| `RadioMonitor.Genre` | Genre des Künstlers (via MusicBrainz) | "alternative rock" |
| `RadioMonitor.Logo` | URL zum Senderlogo | "https://cdn.radio.de/images/broadcasts/..." |
| `RadioMonitor.BandFormed` | Gründungsjahr (nur bei Bands) | "1995" |
| `RadioMonitor.BandMembers` | Aktuelle Mitglieder (nur bei Bands) | "Chad Kroeger, Mike Kroeger, Ryan Peake, Daniel Adair" |
| `RadioMonitor.MBDurationMs` | Von MusicBrainz ermittelte Songdauer in Millisekunden | "175000" |
| `RadioMonitor.MBDurationS` | Von MusicBrainz ermittelte Songdauer in Sekunden | "175" |
| `RadioMonitor.TimeoutTotal` | Aktueller gesetzter Timeout in Sekunden | "160" |
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

### Beispiel 6: Aktuelle API-Daten (Debug)
```xml
<label>API: $INFO[Window(Home).Property(RadioMonitor.ApiData)]</label>
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

### API-Whitelist und MB-Schwelle
- API-Now-Playing wird nur verwendet, wenn die Quelle aus einem whitelisted Addon stammt (`plugin.audio.radiode`, `plugin.audio.radio_de_light`, `plugin.audio.tunein2017`).
- Für alle anderen Quellen werden keine radio.de/TuneIn-API-Calls ausgeführt.
- Die Artist/Title-Entscheidung bleibt konservativ: MusicBrainz nutzt kombinierte Bewertung (`MB score * artist similarity`) und akzeptiert Korrekturen erst oberhalb der Schwellen (`MIN_SCORE=85`, `THRESHOLD=0.7`).
- Spezieller No-Song-Fall: Wenn alle MB-Kandidaten `score=0` haben, wird API nur bei Songwechsel oder ohne validen ICY-Kandidaten uebernommen; sonst werden Artist/Title absichtlich nicht gesetzt.

### Modulstruktur

| Modul | Inhalt |
|-------|--------|
| `metadata.py` | Metadaten-Parsing & Normalisierung, ICY-Extraktion, Artist-Varianten |
| `service.py` | PlayerMonitor, RadioMonitor, Service-Steuerung, Property-Management |
| `musicbrainz.py` | Alle MusicBrainz-API-Funktionen, Album-Auswahl, Artist-Info |
| `radiode.py` | radio.de API Titel-Parser |
| `constants.py` | API-URLs, Property-Namen (_P), Timeouts, Regex |
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
- Bei verschlüsselten Streams (HTTPS) können manche Server keine ICY-Metadaten liefern
- Künstler ohne Einträge in fanart.tv oder theaudiodb liefern keine Hintergrundbilder für Artist Slideshow

## Lizenz

MIT License - siehe LICENSE.txt

## Danksagung

Dieses Addon wurde ursprünglich für das "Radio.de light" Addon von Publish3r entwickelt und zu einem universellen Service für alle Audio-Streams erweitert.

