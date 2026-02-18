# Radio.de Light Monitor Service

Dieses Service-Addon überwacht Radio-Streams und liest die Metadaten (Titel, Interpret, etc.) korrekt aus.

## Features

- ✅ Automatische Erkennung von Radio-Streams (HTTP/HTTPS)
- ✅ Auslesen von ICY-Metadaten (Icecast)
- ✅ Trennung von Artist und Title
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
| `RadioMonitor.StreamTitle` | Vollständiger StreamTitle | "Queen - Bohemian Rhapsody" |
| `RadioMonitor.Genre` | Genre des Senders | "Pop" |
| `RadioMonitor.Album` | Album (falls verfügbar) | "A Night at the Opera" |
| `RadioMonitor.Logo` | Logo URL (zukünftig) | - |

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

1. Kopiere den Ordner `service.monitor.radio_de_light` nach:
   - Windows: `%APPDATA%\Kodi\addons\`
   - Linux: `~/.kodi/addons/`
   - Portable: `<Kodi-Ordner>\portable_data\addons\`

2. Starte Kodi neu oder gehe zu:
   - Einstellungen → Addons → Meine Addons → Dienste
   - Aktiviere "Radio.de Light Monitor Service"

3. Das Service startet automatisch und läuft im Hintergrund

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

Aktiviere Debug-Logging in Kodi:
1. Einstellungen → System → Logging
2. "Debug-Logging aktivieren"
3. Log-Datei ansehen: `kodi.log`
4. Suche nach `[Radio.de Light Monitor Service]`

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

## Credits

Entwickelt für das "Radio.de light" Addon von Publish3r
