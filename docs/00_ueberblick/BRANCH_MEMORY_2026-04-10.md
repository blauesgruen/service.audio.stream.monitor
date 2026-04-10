# Branch Memory (2026-04-10)

Zweck: dauerhafte Notizen aus dem bisherigen Feature-Branch, damit beim Neustart keine fachlichen Erkenntnisse verloren gehen.

## Kernentscheidung (ab jetzt)

- QF wird **aus ASM ausgelagert** und extern gepflegt.
- ASM soll nur noch:
  - den **Sendernamen** uebergeben
  - **Songdaten** empfangen (Artist/Title + Status/Meta)
- Keine zusaetzliche manuelle Sendersteuerung; Verhalten bleibt daten-/regelgetrieben.

## Erkenntnisse zur Quellenbehandlung (ASM-Kern)

1. Rohquellen sind getrennt zu betrachten: `icy`, `api`, `musicplayer`.
2. Vor jeder Entscheidung braucht es harte Plausibilitaetsfilter:
   - generische Texte/Jingles/Hotlines/Nachrichten raus
   - unvollstaendige Paare raus
3. Stations-/Show-Branding darf nie als Song gelten:
   - normalisierter Stationsnamen-Abgleich gegen Kandidaten
   - Beispiele: `MDR JUMP POP - Die Abendshow`, `NDR 90,3 - Wir sind Hamburg`
4. QF-Hits duerfen schnelle Labels setzen (Sofort-Feedback), MB-Anreicherung bleibt nachgelagert.
5. Analyse-Labels muessen den **aktuellen** Entscheidungsstand spiegeln (kein stale Source-State).
6. Source-Policy und Senderprofile bleiben sinnvoll:
   - stabilisieren Quellenwechsel
   - reduzieren Flattern in Programm-/Jingle-Phasen
7. MB bleibt wichtig fuer Qualitaet/Anreicherung, aber Live-Erkennung darf nicht an MB-Latenz haengen.

## Was sich in diesem Branch bewaehrt hat

- QF-Sofort-Label aktualisiert direkt die Analysis-Properties.
- provider_finder-Hits werden vor QF-Weitergabe auf Song-Plausibilitaet geprueft.
- eigener Reject-Grund fuer Stationsname-Match verbessert Debugbarkeit.

## Neustart-Leitplanken fuer die neue Architektur

1. ASM hat eine kleine, klare Schnittstelle zu externem QF-Service.
2. ASM bleibt Owner von:
   - Source-Policy
   - Song-Ende-Logik
   - MB-Verifikation/Anreicherung
   - Label-/Property-Management
3. Externer QF-Service bleibt Owner von:
   - Sendername -> Song-Findung
   - Herkunfts-/Treffer-Metadaten
4. Trigger-Regel fuer QF-Requests:
   - nur bei aktivem HTTP/HTTPS-Audio-Stream
   - und/oder nur bei expliziter ASM-Anfrage (je nach finalem Modus)

