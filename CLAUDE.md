# CLAUDE.md – service.audio.stream.monitor

Kodi-Service-Addon (Python): überwacht Audio-Streams, liest ICY-Metadaten
und MusicBrainz-Daten aus, setzt Ergebnisse als Kodi Window-Properties (`RadioMonitor.*`).

---

## Projektspezifische Regeln

### `_P`-Klasse nicht selbst von replace_all erfassen lassen
Die Klasse `_P` enthält die Window-Property-Konstantennamen.
Bei `replace_all`-Operationen auf Strings mit `_P.` sicherstellen,
dass der neu eingefügte Code nicht selbst ersetzt wurde.

### Terminologie: "Artist" ≠ "AS"
- `Artist` = Interpret des Songs (z.B. "Queen")
- `AS` = AirPlay Sender (technischer Kodi-Begriff)

### Absichtlich unterschiedliche Property-Blöcke nicht zusammenführen
Ähnlich aussehende Property-Setting-Blöcke können absichtlich verschieden sein
(unterschiedliche Bedingungen, unterschiedliche Reihenfolge).
Vor jeder Zusammenführung fragen.

### Property-Reihenfolge beim Setzen ist relevant
Die Reihenfolge der `xbmcgui.Window.setProperty()`-Aufrufe nicht ohne Grund ändern.

### Stationsname-Filterung ist kein Bug
ICY-Strings, die nur den Sendernamen enthalten, werden absichtlich verworfen
→ leere Artist/Title-Properties. Das ist korrekt.
