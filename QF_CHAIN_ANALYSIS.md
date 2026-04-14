# QF-Kette (ASM-QF) - Umfassende Analyse

> Statushinweis (2026-04-14): Dieses Dokument ist eine historische Fehleranalyse.
> Es ist **nicht** die autoritative Vertragsquelle fuer den aktuellen Runtime-Vertrag.
> Verbindlich sind:
> - `TECHNISCHE_DOKUMENTATION.md` (Abschnitt `6.3 ASM-QF Runtime-Contract`)
> - `docs/00_ueberblick/ASM_QF_SHARED_DB_CONTRACT.md` (Ergaenzender Runtime-Property-Vertrag)
>
> Insbesondere gilt aktuell: Jede `RadioMonitor.QF.Request.Id` muss genau eine terminale Response erhalten
> (`hit`, `no_hit`, `resolve_error`, `error`, `timeout`, `superseded`, `cancelled`).
>
> Zusatzstand (aktuelle Runtime):
> - API-Stationsnamen aus radio.de/TuneIn werden nur mit Source-Proof (verifizierter Plugin-Start) autoritativ gesetzt.
> - ASM stabilisiert `RadioMonitor.QF.Request.Station` per Session-Anchor bis zum echten Streamwechsel/Stop.
> - Bei langsamer terminaler QF-Entscheidung kann um `QF_NO_RESPONSE_FALLBACK_S` (~25s) kurz `fresh_reason=stale_response` auftreten.
> - ASM setzt kein eigenes `RadioMonitor.QF.Response.StationUsed`; `stationused`-Skins mappen i. d. R. auf `Response.Source`/`Response.Meta`.

## 1. Übersicht der QF-Logik-Kette

Die QF-Integration im Audio Stream Monitor hat mehrere kritische Pfade:

### 1.1 Hauptplatzierungen der QF-Logik

**service.py - Zentrale QF-Verwaltung:**
- `_qf_enabled`: Flag zur Aktivierung/Deaktivierung
- `_last_qf_request_id`: Request-ID für Frische-Prüfung
- `_last_qf_response_id`: Response-ID für Match-Erkennung
- `_qf_no_hit_hold_active`: Hold-Zustand bei "no_hit"
- `_last_qf_nonfresh_log_ts`: Deduplication von Logs
- `_last_policy_context`: Kontext mit aktuellen QF-Paaren

---

## 2. QF-Request-Flow (Ticken der Requests)

### 2.1 `_tick_qf_request()` (Zeile 1853-1899)

**Zweck:** Sendet periodisch neue QF-Requests an das ASM-QF-Addon

**Kritische Logik:**

```python
if self._last_qf_request_id and not station_changed:
    snapshot = self._qf_response_snapshot()
    if not snapshot.get('fresh'):
        request_age_s = max(0.0, float(now_ts - float(self._last_qf_request_ts or 0.0)))
        if request_age_s < float(QF_NO_RESPONSE_FALLBACK_S):
            return  # Warte auf Response, starte keine neue Request
```

**FEHLER #1 - Blocking-Logik Problem:**
- Die Bedingung `if request_age_s < float(QF_NO_RESPONSE_FALLBACK_S)` blockiert neue Requests
- Problem: Wenn QF länger als `QF_NO_RESPONSE_FALLBACK_S` (5 Sekunden Default) auf die Antwort wartet, wird zwar eine neue Request gesendet, aber die **alte Response wird trotzdem noch verarbeitet**
- **Coderest:** Die frühere Response kann als "fresh" interpretiert werden, obwohl sie zu alt ist

### 2.2 `_send_qf_request()` (Zeile 1782-1801)

**Zweck:** Erstellt und sendet eine QF-Request

**Kritische Fehler:**

```python
now_ts = time.time()
self._qf_request_seq = (self._qf_request_seq + 1) % 1000000
request_id = f"asm-{int(now_ts * 1000)}-{self._qf_request_seq}"
WINDOW.setProperty(_P.QF_REQUEST_STATION, station)
WINDOW.setProperty(_P.QF_REQUEST_MODE, str(mode or 'asm_auto'))
WINDOW.setProperty(_P.QF_REQUEST_TS, str(int(now_ts)))
# Request-ID immer zuletzt setzen !!!
WINDOW.setProperty(_P.QF_REQUEST_ID, request_id)
```

**FEHLER #2 - Race Condition möglich:**
- Request-ID wird zuletzt gesetzt, aber die anderen Properties sind bereits sichtbar
- Wenn ASM-QF zwischen den Property-Setzungen prüft, könnte es inconsistente Requests sehen
- **Risiko:** Sehr klein (nanosekunden-Fenster), aber theoretisch möglich

**FEHLER #3 - Zeitstempel-Inconsistenz:**
```python
self._last_qf_request_ts = now_ts  # Store float
WINDOW.setProperty(_P.QF_REQUEST_TS, str(int(now_ts)))  # Store int (Millisekunden-Präzision verloren)
```
- `_last_qf_request_ts` wird als Float gespeichert, aber in Properties als Int
- Kann zu Timing-Fehlern führen, wenn Zeitdifferenzen berechnet werden

---

## 3. QF-Response-Snapshot & Frische-Prüfung

### 3.1 `_qf_response_snapshot()` (Zeile 1634-1704)

**Zweck:** Liest aktuelle QF-Response-Properties und berechnet Frische-Metriken

**FEHLER #4 - Fallback-Logik für Zeitstempel:**

```python
if request_ts is None and self._last_qf_request_ts:
    request_ts = float(self._last_qf_request_ts)  # Fallback zu lokaler Zeit
```

**Problem:** 
- Wenn die Response keinen Zeitstempel hat, wird auf `self._last_qf_request_ts` zurückgegriffen
- Dies kann zu falschen Frische-Metriken führen, wenn die Response-Verarbeitung verzögert ist
- **Coderest:** `_parse_qf_epoch_ts()` ignoriert invalid/future timestamps, aber keine Validierung gegen lokale Uhr

### 3.2 Fresh-Reason Klassifikation (Zeile 1673-1691)

```python
fresh_reason = 'id_match' if fresh else 'id_mismatch'
if not fresh:
    if not response_id:
        fresh_reason = 'missing_response_id'
    elif not self._last_qf_request_id:
        fresh_reason = 'missing_request_id'
    elif response_id != self._last_qf_request_id:
        if client_gap is not None:
            if client_gap <= float(QF_NO_RESPONSE_FALLBACK_S):
                fresh_reason = 'id_mismatch_ts_ok'
            else:
                fresh_reason = 'stale_response'
        elif gap_source == 'server_ts':
            if (gap_raw or 0.0) <= float(QF_NO_RESPONSE_FALLBACK_S):
                fresh_reason = 'id_mismatch_waiting'
            else:
                fresh_reason = 'stale_response'
        else:
            fresh_reason = 'id_mismatch_no_ts'
```

**FEHLER #5 - Unvollständige Fallback-Kette:**
- Bei `client_gap is None` AND `gap_source != 'server_ts'` wird `fresh_reason = 'id_mismatch_no_ts'` gesetzt
- **Coderest:** Falls `gap_source == 'none'` (keine Zeitstempel), gibt es keine Timeout-Prüfung
- Resultat: Non-fresh Responses können unbegrenzt als valide verwendet werden, wenn Zeit-Properties fehlen

---

## 4. QF-Hit-Pair-Auswertung

### 4.1 `_current_qf_hit_pair()` (Zeile 1758-1780)

```python
snapshot = self._qf_response_snapshot()
if not snapshot.get('fresh'):
    usable_nonfresh = self._is_qf_usable_nonfresh_hit(snapshot)
    if require_fresh:
        if not (allow_recent_nonfresh_hit and usable_nonfresh):
            return ('', '')
    elif not usable_nonfresh:
        return ('', '')
if snapshot.get('status') != 'hit':
    return ('', '')
artist = str(snapshot.get('artist') or '').strip()
title = str(snapshot.get('title') or '').strip()
if not (artist and title):
    return ('', '')
return (artist, title)
```

**FEHLER #6 - Doppelter Fresh-Check möglich:**
- `require_fresh=False` erlaubt non-fresh Hits, aber `_is_qf_usable_nonfresh_hit()` macht NOCHMAL frische-Prüfung
- **Redundanz:** Wenn `require_fresh=False`, sollte die Usability-Prüfung einfacher sein
- **Coderest:** Der `allow_recent_nonfresh_hit` Parameter wird ignoriert bei `require_fresh=False`

### 4.2 `_is_qf_usable_nonfresh_hit()` (Zeile 1706-1727)

```python
if fresh_reason not in ('id_mismatch_waiting', 'id_mismatch_ts_ok'):
    return False
```

**FEHLER #7 - Race-Condition nicht berücksichtigt:**
- Nur 2 Frische-Gründe erlaubt: `id_mismatch_waiting` und `id_mismatch_ts_ok`
- **Fehler:** `stale_response` könnte bei zeitlich schnellen Wiederholungen kurzzeitig zwischen Request-Timeout und Fallback-Aktivierung als "brauchbar" interpretiert werden
- **Coderest:** Keine explizite Race-Guard für Request-Überhol-Szenarien

---

## 5. QF-Authoritative Zustand & Fallback

### 5.1 `_is_qf_authoritative()` (Zeile 1751-1756)

```python
return bool(self._qf_enabled and self.is_playing and not self._is_qf_fallback_exception())
```

**FEHLER #8 - Exception Definition ist ungenau:**

```python
def _is_qf_fallback_exception(self):
    if not self._qf_enabled or not self.is_playing:
        return True
    if not self._last_qf_request_id:
        return True
    
    snapshot = self._qf_response_snapshot()
    if snapshot.get('fresh'):
        return snapshot.get('status') in ('resolve_error', 'error', 'timeout')
    
    try:
        age_s = max(0.0, float(time.time() - float(self._last_qf_request_ts or 0.0)))
    except Exception:
        age_s = float(QF_NO_RESPONSE_FALLBACK_S)
    return age_s >= float(QF_NO_RESPONSE_FALLBACK_S)
```

**FEHLER #9 - Status-Werte nicht vollständig geprüft:**
- Nur `resolve_error`, `error`, `timeout` bei Fresh-Responses werden als Fallback-Gründe erkannt
- **Coderest:** Status `'no_hit'` ist normales Verhalten, aber bei `'unknown'` oder anderen Status-Werten wird nicht automatisch gefallback
- **Problem:** Wenn ASM-QF einen Status sendet, der nicht in der Liste ist, wird QF als autoritativ behandelt, obwohl es fehlgeschlagen hat

---

## 6. QF-No-Hit-Hold Mechanismus

### 6.1 `_sync_qf_result_property()` (Zeile 1901-2044)

**Komplexe Hold-Logik bei "no_hit"-Status:**

```python
if snapshot.get('status') != 'hit':
    has_visible_song = bool(
        (WINDOW.getProperty(_P.ARTIST) or '').strip()
        or (WINDOW.getProperty(_P.TITLE) or '').strip()
        or self._last_qf_result
    )
    if qf_authoritative and has_visible_song:
        self._start_qf_no_hit_hold()
```

**FEHLER #10 - Redundante Hold-Prüfung in `_is_qf_no_hit_hold_active()`:**

```python
def _is_qf_no_hit_hold_active(self):
    if not self._qf_no_hit_hold_active:
        return False
    age_s = max(0.0, float(time.time() - float(self._qf_no_hit_hold_since_ts or 0.0)))
    if age_s < float(self._qf_no_hit_hold_s):
        return True
    self._qf_no_hit_hold_active = False
    self._qf_no_hit_hold_since_ts = 0.0
    # ... log entry
    return False
```

**FEHLER #11 - State-Reset Problem:**
- Hold wird innerhalb von `_is_qf_no_hit_hold_active()` geleert
- Wenn die Methode mehrmals kurz hintereinander aufgerufen wird, kann der Log-Eintrag mehrfach geschrieben werden
- **Coderest:** `_log_qf_diag('hold_end', ...)` kann mehrfach mit gleichen Daten geschrieben werden

### 6.2 Hold-Park-Trigger (Zeile 4248-4262)

```python
if (
    source_changed_trigger
    and trigger_reason == self.TRIGGER_QF_CHANGE
    and qf_no_hit_hold_active
    and str(last_winner_source or '').startswith('asm-qf')
    and not (current_qf_pair[0] and current_qf_pair[1])
):
    self._log_qf_diag('hold_park_trigger', {...})
    source_changed_trigger = False
    initial_source_pending = False
```

**FEHLER #12 - QF-Exklusivität nicht beachtet:**
- Der Park-Trigger prüft `and not (current_qf_pair[0] and current_qf_pair[1])`
- **Problem:** Wenn QF währenddessen einen Hit liefert, wird dieser geparkte Trigger nie wiederhergestellt
- Der Trigger ist für immer verloren

---

## 7. QF-Kandidaten in parse_stream_title() (Zeile 3612-3632)

```python
qf_valid = False
if self._qf_enabled:
    qf_locked = str(getattr(self, '_parse_locked_source', '') or '').startswith('asm-qf')
    qf_require_fresh = not (qf_authoritative or qf_locked)
    qf_artist, qf_title = self._current_qf_hit_pair(invalid, require_fresh=qf_require_fresh)
    qf_artist = str(qf_artist or '').strip()
    qf_title = str(qf_title or '').strip()
    if qf_artist and qf_title:
        qf_valid = True
        candidates.append({'source': 'asm-qf', 'artist': qf_artist, 'title': qf_title})
    if qf_valid:
        if qf_artist != qf_title:
            candidates.append({'source': 'asm-qf_swapped', 'artist': qf_title, 'title': qf_artist})
        candidates[:] = [c for c in candidates if str(c.get('source', '')).startswith('asm-qf')]
    elif qf_authoritative:
        log_info("ASM-QF autoritativ: kein valider QF-Song -> kein Song")
        self._set_last_song_decision('', None, None)
        return None, None, '', '', '', '', 0
```

**FEHLER #13 - Swapped-Duplikate möglich:**
```python
if qf_artist != qf_title:
    candidates.append({'source': 'asm-qf_swapped', 'artist': qf_title, 'title': qf_artist})
```

- **Coderest:** Wenn `qf_artist == qf_title` (Single-Name Artist), wird KEIN swapped Kandidat hinzugefügt
- **Problem:** Aber vorher wurde nur `qf_valid` gecheckt, nicht ob swapped bereits vorhanden ist
- **Risiko:** Bei doppeltem Aufruf (sollte nicht vorkommen) könnten Duplikate entstehen

**FEHLER #14 - Exklusiv-Modus zu früh aktiv:**
```python
candidates[:] = [c for c in candidates if str(c.get('source', '')).startswith('asm-qf')]
```
- Sobald `qf_valid=True`, werden ALL anderen Kandidaten gelöscht
- **Problem:** Wenn QF nur eine Sekunde später einen `no_hit` liefert, sind API/ICY-Alternativen bereits weg
- Dies kann zu längeren "kein Song"-Phasen führen

---

## 8. Trigger-Logik mit QF (Zeile 4215-4230)

```python
stream_title_changed_for_policy = stream_title_changed
if str(last_winner_source or '').startswith('asm-qf'):
    stream_title_changed_for_policy = False

source_changed_trigger, trigger_reason = self._determine_source_change_trigger(
    last_winner_source,
    last_winner_pair,
    current_mp_pair,
    current_api_pair,
    current_icy_pair,
    station_name,
    stream_title_changed_for_policy,
    ...
    current_qf_pair=current_qf_pair
)
```

**FEHLER #15 - ICY StreamTitle wird ignoriert bei QF-Lock:**
- Wenn `last_winner_source.startswith('asm-qf')`, wird `stream_title_changed_for_policy = False` gesetzt
- **Absicht:** ICY-StreamTitle soll keinen Trigger bewirken
- **Coderest:** Aber logs werden für diesen `stream_title_changed` nicht generiert - Silent Ignore
- **Problem:** Bei QF-Fehlern können Trigger völlig stumm bleiben

---

## 9. First-Paint QF-Optimization (Zeile 4342-4358)

```python
if (
    self._qf_enabled
    and qf_authoritative
    and trigger_reason == self.TRIGGER_QF_CHANGE
    and current_qf_pair[0]
    and current_qf_pair[1]
):
    WINDOW.clearProperty(_P.MBID)
    self.set_property_safe(_P.TITLE, current_qf_pair[1])
    self.set_property_safe(_P.ARTIST, current_qf_pair[0])
    self.set_property_safe(_P.ARTIST_DISPLAY, current_qf_pair[0])
```

**FEHLER #16 - Race zwischen First-Paint und MB-Lookup:**
- MBID wird sofort geleert, dann werden Artist/Title gesetzt
- **Problem:** AS hört auf Artist-Änderung (Trigger), aber MBID ist NOCH LEER
- Wenn AS sofort auf MBID reagiert, bekommt es einen leeren Wert
- **Timeline-Problem:** MB-Lookup findet später das MBID, aber AS hat bereits mit leerer MBID reagiert

### Workaround ist vorhanden (korrekt):
- Der Code setzt zuerst MBID, DANN Artist
- Aber in First-Paint wird MBID gelöscht, BEVOR Artist gesetzt wird
- **Fehler:** Reihenfolge ist falsch für First-Paint

---

## 10. QF-Prefill der Properties (Zeile 2039-2044)

```python
if label != self._last_qf_result:
    self._last_qf_result = label
    self.set_property_safe(_P.QF_RESULT, label)
    if self._qf_enabled:
        self.set_property_safe(_P.ARTIST, artist)
        self.set_property_safe(_P.ARTIST_DISPLAY, artist)
        self.set_property_safe(_P.TITLE, title)
```

**FEHLER #17 - Doppeltes Prefill möglich:**
- `_sync_qf_result_property()` schreibt Prefill
- `parse_stream_title()` schreibt First-Paint
- **Coderest:** Wenn beide Wege gleichzeitig aktiv sind, können Properties mehrfach überschrieben werden

---

## 11. Zusammengefasste Fehler & Codereste

| # | Fehler | Ort | Severity | Auswirkung |
|---|--------|-----|----------|-----------|
| 1 | Blocking-Logik bei Request-Timeout | `_tick_qf_request()` | MEDIUM | Requests können gehemmt werden |
| 2 | Race Condition bei Property-Reihenfolge | `_send_qf_request()` | LOW | Theoritisch möglich |
| 3 | Zeitstempel-Inconsistenz (float vs int) | `_send_qf_request()` | MEDIUM | Timing-Fehler möglich |
| 4 | Fallback zu lokaler Zeit unsicher | `_qf_response_snapshot()` | MEDIUM | False Frische-Metriken |
| 5 | Unvollständige Fallback-Kette bei no_ts | `_qf_response_snapshot()` | HIGH | Non-fresh bleibt unbegrenzt valide |
| 6 | Doppelte Fresh-Prüfung redundant | `_current_qf_hit_pair()` | LOW | Redundanz |
| 7 | Race-Condition bei Status-Check | `_is_qf_usable_nonfresh_hit()` | MEDIUM | Request-Überholung möglich |
| 8 | Exception-Definition ungenau | `_is_qf_fallback_exception()` | HIGH | Unknown Status = QF autoritativ |
| 9 | Nicht alle Status-Werte behandelt | `_is_qf_fallback_exception()` | HIGH | Silent Fallback-Miss |
| 10 | State-Reset innerhalb Prüf-Methode | `_is_qf_no_hit_hold_active()` | MEDIUM | Log-Duplikate, State-Chaos |
| 11 | Geparkte Trigger können verloren gehen | Hold-Park-Trigger | HIGH | Trigger permanent verloren |
| 12 | Redundante swapped-Duplikate | parse_stream_title() | LOW | Code-Smell |
| 13 | Exklusiv-Modus löscht Fallbacks zu früh | parse_stream_title() | HIGH | Längere no-hit-Phasen |
| 14 | Silent Ignore bei ICY StreamTitle | metadata_worker() | MEDIUM | Fehlende Diagnostik |
| 15 | First-Paint MBID-Reihenfolge falsch | metadata_worker() | HIGH | AS bekommt leere MBID |
| 16 | Doppeltes Prefill möglich | _sync_qf_result_property() | MEDIUM | Property-Flackering |

---

## 12. Empfohlene Fixes

### Fix #1: Bessere Request-Blockade
```python
# Statt nur on age, check auch response status
if self._last_qf_request_id and not station_changed:
    snapshot = self._qf_response_snapshot()
    if not snapshot.get('fresh'):
        # Nur blockieren wenn Response pending ist
        if snapshot.get('response_id') == self._last_qf_request_id:
            # Gleiche Request-ID -> noch nicht geantwortet
            return
```

### Fix #2: Zeitstempel Konsistenz
```python
now_ts = time.time()
now_ts_ms = int(now_ts * 1000)
now_ts_int = int(now_ts)
WINDOW.setProperty(_P.QF_REQUEST_TS, str(now_ts_ms))  # Konsistent mit Response
self._last_qf_request_ts = now_ts  # Behalte float für Berechnungen
```

### Fix #3: First-Paint MBID Reihenfolge
```python
# Title ZUERST, dann MBID (empty), dann Artist
if current_qf_pair[1]:
    self.set_property_safe(_P.TITLE, current_qf_pair[1])
WINDOW.clearProperty(_P.MBID)  # NACH Title, VOR Artist
self.set_property_safe(_P.ARTIST, current_qf_pair[0])
```

### Fix #4: Hold-Park-Trigger Restoration
```python
# Wenn Trigger geparkt wurde und QF später einen Hit liefert
if (
    # ... hold park conditions
):
    self._log_qf_diag('hold_park_trigger', {...})
    source_changed_trigger = False
    initial_source_pending = False
    # WICHTIG: Trigger als "pending" markieren für nächsten Zyklus
    self._pending_qf_trigger_restoration = True
```

### Fix #5: Status-Validierung
```python
KNOWN_QF_STATUS = {'hit', 'no_hit', 'resolve_error', 'error', 'timeout', 'unknown'}
if snapshot.get('status') not in KNOWN_QF_STATUS:
    log_warning(f"Unknown QF status: {snapshot.get('status')}")
    return True  # Fallback to API/ICY
```

---

## Fazit

Die QF-Integration hat **mehrere kritische Fehler** an der Schnittstelle zwischen Request-Timing, Response-Parsing und Kandidaten-Auswahl. Die meisten sind nicht sofort fatal, aber können zu subtilen Timing-Problemen, Lost-Triggers und False-State-Szenarien führen.

**Höchste Priorität:**
1. Fix #8 (Status-Validierung) - verhindert Silent Failures
2. Fix #5 (First-Paint MBID) - verhindert AS-Integration Fehler
3. Fix #11 (Hold-Park Restoration) - verhindert Lost Triggers


