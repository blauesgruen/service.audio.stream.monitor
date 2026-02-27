# Audio Stream Monitor

Dieses Service-Addon überwacht **alle HTTP/HTTPS Audio-Streams** und liest die Metadaten (Titel, Interpret, etc.) korrekt aus.

**Funktioniert mit jedem Addon**, das Audio-Streams abspielt (Radio.de, TuneIn, Musik-Streaming, etc.).

## Features

- ✅ Universelle Unterstützung für alle HTTP/HTTPS Audio-Streams
- ✅ Automatische Erkennung von Radio- und Musik-Streams
- ✅ Auslesen von ICY-Metadaten (Icecast/Shoutcast)
- ✅ Trennung von Artist und Title
- ✅ Automatisches Löschen der Properties beim Stoppen
- ✅ Verhindert alte Metadaten beim Addon-Wechsel
- ✅ Spezielle Optimierungen für radio.de API

## Verfügbare Window Properties

Das Service-Addon setzt folgende Properties, die in der Kodi-Skin verwendet werden können:

| Property | Beschreibung | Beispiel |
|----------|--------------|----------|
| `RadioMonitor.Playing` | "true" wenn Radio läuft | true |
| `RadioMonitor.Station` | Name des Radiosenders | "Bayern 3" |
| `RadioMonitor.Title` | Aktueller Song-Titel | "Bohemian Rhapsody" |
| `RadioMonitor.Artist` | Aktueller Interpret | "Queen" |
| `RadioMonitor.MBID` | MusicBrainz Artist ID | "0383dadf-2a4e-4d10-a46a-e9e041da8eb3" |
| `RadioMonitor.StreamTitle` | Vollständiger StreamTitle | "Queen - Bohemian Rhapsody" |
| `RadioMonitor.Genre` | Genre des Senders | "Pop" |
| `RadioMonitor.Album` | Album (falls verfügbar) | "A Night at the Opera" |
| `RadioMonitor.Logo` | URL zum Senderlogo | "https://cdn.radio.de/images/broadcasts/..." |

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

1. Kopiere den Ordner `service.monitor.radio_de_light` in dein Kodi Addon-Verzeichnis:
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

1. **Log-Datei ansehen:** Die `kodi.log` befindet sich im `temp`-Ordner deines Kodi-Benutzerdatenverzeichnisses.
2. **Suche nach:** `[Audio Stream Monitor]`
3. **Für detaillierte Logs:** Aktiviere "Debug-Logging" in Kodi unter `Einstellungen → System → Logging`.

## Kompatibilität

- Kodi 19 (Matrix) und höher
- Alle Plattformen (Windows, Linux, Android, etc.)
- Funktioniert mit allen Radio-Streams, die ICY-Metadaten unterstützen

## Bekannte Limitierungen

- Nicht alle Radio-Streams senden ICY-Metadaten
- Manche Sender senden nur den Sendernamen, keinen aktuellen Titel
- Bei verschlüsselten Streams (HTTPS) können manche Server keine ICY-Metadaten liefern

## Lizenz

MIT License - siehe LICENSE.txt

## Danksagung

Dieses Addon wurde ursprünglich für das "Radio.de light" Addon von Publish3r entwickelt und zu einem universellen Service für alle Audio-Streams erweitert.
