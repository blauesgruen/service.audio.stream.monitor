# Audio Stream Monitor

Dieses Service-Addon überwacht **alle HTTP/HTTPS Audio-Streams** und liest die Metadaten (Titel, Interpret, etc.) korrekt aus.

**Funktioniert mit jedem Addon**, das Audio-Streams abspielt (Radio.de, TuneIn, Musik-Streaming, etc.).

## Features

- ✅ Universelle Unterstützung für alle HTTP/HTTPS Audio-Streams
- ✅ Automatische Erkennung von Radio- und Musik-Streams
- ✅ Auslesen von ICY-Metadaten (Icecast/Shoutcast)
- ✅ Intelligente Trennung von Artist und Title (mehrere Trennzeichen, 'von'-Format, last-separator-Variante)
- ✅ Stationsname-Filterung: ICY-Strings, die nur den Sendernamen enthalten, werden nicht als Artist/Title übernommen
- ✅ MusicBrainz-Abgleich zur Validierung und Korrektur von Artist, Title, Album, AlbumDate, FirstRelease und MBID
- ✅ Erweitertes Artist-Matching: CamelCase-Splitting ("DeBurgh" → "De Burgh"), Komma-Umkehr, Apostroph-Normalisierung und tokenbasierter Fallback
- ✅ Intelligente Album-Auswahl: nur Releases des gewählten Best-Recordings werden berücksichtigt; bevorzugt wird das erste passende Studioalbum (Special-/Exclusive-Editionen werden bei Gleichstand nachrangig behandelt)
- ✅ Klammern-Bereinigung im Titel vor MB-Suche: Metadaten-Tags wie "(Radio Edit)" oder "(Remastered 2011)" werden iterativ entfernt, inhaltliche Klammern wie "(Love theme)" bleiben erhalten
- ✅ radio.de Now-Playing API als Fallback bei fehlenden ICY-Metadaten
- ✅ Aktualitäts-Check für API-Daten: veraltete API-Antworten (noch alter Song) werden verworfen
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
| `RadioMonitor.Genre` | Genre des Senders | "Pop" |
| `RadioMonitor.Logo` | URL zum Senderlogo | "https://cdn.radio.de/images/broadcasts/..." |
| `RadioMonitor.BandFormed` | Gründungsjahr (nur bei Bands) | "1995" |
| `RadioMonitor.BandMembers` | Aktuelle Mitglieder (nur bei Bands) | "Chad Kroeger, Mike Kroeger, Ryan Peake, Daniel Adair" |

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
- Funktioniert mit allen Radio-Streams, die ICY-Metadaten unterstützen

## Bekannte Limitierungen

- Nicht alle Radio-Streams senden ICY-Metadaten
- Manche Sender senden nur den Sendernamen statt Interpret/Titel – dieser wird korrekt gefiltert und nicht als Artist oder Title übernommen; alle Properties bleiben in diesem Fall leer
- Bei verschlüsselten Streams (HTTPS) können manche Server keine ICY-Metadaten liefern

## Lizenz

MIT License - siehe LICENSE.txt

## Danksagung

Dieses Addon wurde ursprünglich für das "Radio.de light" Addon von Publish3r entwickelt und zu einem universellen Service für alle Audio-Streams erweitert.
