# CLAUDE.md – Entwicklungsregeln für service.audio.stream.monitor

## Projektkontext

Kodi-Service-Addon (Python), das Audio-Streams überwacht und Metadaten
(Artist, Title, Album etc.) via ICY-Protokoll und MusicBrainz API ausliest.
Die Metadaten werden als Kodi Window-Properties gesetzt (`RadioMonitor.*`).

---

## Projektspezifische Regeln

### 1. `_P`-Klasse nicht selbst von replace_all erfassen lassen
Die Klasse `_P` enthält die Window-Property-Konstantennamen.
Bei `replace_all`-Operationen auf Strings, die `_P.` enthalten:
**Sicherstellen, dass der neu eingefügte Code nicht selbst ersetzt wurde.**
Vor jedem Commit: `git diff` lesen und bestätigen lassen.

### 2. Terminologie: "Artist" ≠ "AS"
- `Artist` = Interpret des Songs (z.B. "Queen")
- `AS` = AirPlay Sender (technischer Begriff aus dem Kodi-Kontext)
Nie verwechseln. Im Code immer den jeweils richtigen Begriff verwenden.

### 3. Absichtlich unterschiedliche Code-Blöcke nicht zusammenführen
Ähnlich aussehende Property-Setting-Blöcke können absichtlich verschieden sein
(z.B. unterschiedliche Bedingungen, unterschiedliche Property-Reihenfolge).
**Vor jeder DRY-Abstraktion:** alle Unterschiede auflisten und fragen,
ob diese Unterschiede beabsichtigt sind.

### 4. Property-Reihenfolge beim Setzen ist relevant
Kodi-Skins lesen Properties in bestimmten Sequenzen.
Die Reihenfolge, in der `xbmcgui.Window.setProperty()` aufgerufen wird,
nicht ohne Grund ändern.

### 5. Kommentare mit "bewusst" / "absichtlich" / "Grund:" sind Warnsignale
Solche Kommentare markieren Stellen, die absichtlich unkonventionell sind.
**Niemals ohne Rückfrage ändern.**

### 6. Stationsname-Filterung ist kein Bug
ICY-Strings, die nur den Sendernamen enthalten, werden absichtlich verworfen
und führen zu leeren Artist/Title-Properties. Das ist korrekt.

---

## Allgemeine Arbeitsregeln

### Scope: Nur das Beauftragte ändern
Kein "Nebenbei-Aufräumen" von Code, der nicht im Auftrag steht.
Kein Hinzufügen von Features, die nicht explizit verlangt wurden (YAGNI).

### Vor Commits
1. `git diff` zeigen und bestätigen lassen
2. Besonders nach `replace_all`: prüfen ob neu eingefügter Code betroffen ist

### Keine vorzeitigen Abstraktionen
Drei ähnliche Codezeilen sind besser als eine falsche Abstraktion.
Erst abstrahieren wenn die Gleichheit bewiesen (nicht nur vermutet) ist.

### Keine Fehlerbehandlung für unmögliche Fälle
Nur an echten Systemgrenzen validieren (User-Input, externe APIs wie MusicBrainz, radio.de).
Interne Funktionen vertrauen ihren Aufrufern.
