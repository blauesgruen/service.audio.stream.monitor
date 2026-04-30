"""
radio.de API - Hilfsfunktionen.

Exportiert Parsing- und Abfragefunktionen fuer radio.de API-Antworten.
"""
from concurrent.futures import ThreadPoolExecutor
import re
from api_client import APIClient
from constants import (
    INVALID_METADATA_VALUES,
    NUMERIC_ID_PATTERN as _NUMERIC_ID_RE,
    RADIODE_SEARCH_API_URL,
    RADIODE_NOWPLAYING_API_URL,
    RADIODE_DETAILS_API_URL,
)
from logger import log_debug, log_info, log_warning


def _clone_api_client(api_client):
    headers = {}
    try:
        headers = dict(getattr(getattr(api_client, 'session', None), 'headers', {}) or {})
    except Exception:
        headers = {}
    try:
        retry_count = int(getattr(api_client, 'retry_count', 3) or 3)
    except Exception:
        retry_count = 3
    return APIClient(headers=headers, retry_count=retry_count)


def _fetch_details_by_slug(api_client, slug):
    resolved_name = None
    det_logo = None
    client = _clone_api_client(api_client)
    try:
        det_response = client.get(
            RADIODE_DETAILS_API_URL, params={'stationIds': slug}, timeout=5
        )
        if det_response.status_code == 200:
            det_data = det_response.json()
            if isinstance(det_data, list) and len(det_data) > 0:
                proper_name = det_data[0].get('name', '')
                if proper_name:
                    resolved_name = proper_name
                found_logo = det_data[0].get('logo300x300', '')
                if found_logo:
                    det_logo = found_logo
    except Exception as e:
        log_debug(f"Fehler bei Details-API: {e}")
    finally:
        try:
            client.close()
        except Exception:
            pass
    return resolved_name, det_logo


def _fetch_nowplaying_by_slug(api_client, slug, station_name, debug_log=None):
    client = _clone_api_client(api_client)
    try:
        np_response = client.get(
            RADIODE_NOWPLAYING_API_URL, params={'stationIds': slug}, timeout=5
        )
        if np_response.status_code == 200:
            np_data = np_response.json()
            if debug_log:
                debug_log('radiode.now_playing.slug', np_data)
            if isinstance(np_data, list) and len(np_data) > 0:
                full_title = np_data[0].get('title', '')
                if full_title:
                    artist, title = parse_radiode_api_title(full_title, station_name)
                    if artist or title:
                        return artist, title
            # Slug bekannt, aber API hat keinen Song geliefert (Programm/Moderation).
            # Keine Stationssuche starten: eine Suche koennte faelschlicherweise
            # einen anderen Sender mit aehnlichem Namen finden und dessen Daten
            # (Logo, Name, Now-Playing) uebernehmen.
            log_debug("Slug bekannt, aber kein aktiver Song via Slug-API (Programm/Moderation)")
    except Exception as e:
        log_debug(f"Fehler bei now-playing via Slug: {e}")
    finally:
        try:
            client.close()
        except Exception:
            pass
    return None, None


def parse_radiode_api_title(full_title, station_name=None):
    """
    Parst radio.de API Titel in mehreren Formaten und gibt (artist, title) zurück.
    Unterstützt u.a.:
    - "ARTIST - TITLE"
    - "TITLE von ARTIST"
    - "TITLE von ARTIST JETZT AUF <STATION>"

    Ungültige Werte werden zu None normalisiert.
    """
    invalid = INVALID_METADATA_VALUES + ['']
    if not full_title:
        return None, None

    text = full_title.strip()
    # Sender-Promo am Ende entfernen: "... JETZT AUF MDR JUMP"
    text = re.sub(r'\s+JETZT\s+AUF\s+.+$', '', text, flags=re.IGNORECASE).strip()

    artist = None
    title = None

    # 1) Standard: "ARTIST - TITLE"
    if ' - ' in text:
        # Bei Mehrfach-Trennern bevorzugt am letzten Trenner teilen:
        # "Artist A - Artist B - Title" -> Artist="Artist A - Artist B", Title="Title"
        if text.count(' - ') > 1:
            parts = text.rsplit(' - ', 1)
        else:
            parts = text.split(' - ', 1)
        artist = parts[0].strip()
        title = parts[1].strip()
    else:
        # 2) MDR-typisch: "TITLE von ARTIST"
        von_match = re.match(r'^(.+?)\s+von\s+(.+)$', text, flags=re.IGNORECASE)
        if von_match:
            title = von_match.group(1).strip()
            artist = von_match.group(2).strip()

    if artist in invalid:
        artist = ''
    if title in invalid or (station_name and title == station_name):
        title = ''
    if title and _NUMERIC_ID_RE.match(title):
        return None, None
    return artist or None, title or None


def get_nowplaying(api_client, plugin_slug, station_name, existing_logo=None, debug_log=None):
    """
    Holt aktuelle Song-Info direkt von der radio.de API.

    Gibt (artist, title, resolved_name, det_logo, search_logo) zurueck:
    - resolved_name: Sendername aus Details-API (None wenn nicht gefunden)
    - det_logo:      Logo aus Details-API (im Aufrufer nur setzen wenn kein Logo vorhanden)
    - search_logo:   Logo aus Stationssuche (im Aufrufer immer setzen wenn nicht None)
    """
    resolved_name = None
    det_logo = None
    search_logo = None

    try:
        # Slug-Quelle: Plugin-URL hat Prioritaet, vorhandenes Logo-URL als Fallback
        slug = plugin_slug
        if not slug and existing_logo:
            logo_match = re.search(r'radio-assets\.com/\d+/([^./?]+)', existing_logo)
            if logo_match:
                slug = logo_match.group(1)

        if slug:
            log_debug(f"Station-Slug: '{slug}' (plugin={bool(plugin_slug)})")
            with ThreadPoolExecutor(max_workers=2) as executor:
                details_future = executor.submit(_fetch_details_by_slug, api_client, slug)
                nowplaying_future = executor.submit(
                    _fetch_nowplaying_by_slug,
                    api_client,
                    slug,
                    station_name,
                    debug_log,
                )
                resolved_name, det_logo = details_future.result()
                artist, title = nowplaying_future.result()
            if artist or title:
                log_info(f"OK now-playing via Slug: {artist} - {title}")
                return artist, title, resolved_name, det_logo, search_logo
            # Bei Netzwerkfehler ebenfalls kein Suchfallback (Sender ist bekannt)
            return None, None, resolved_name, det_logo, search_logo

        # Sendernamen fuer die Suche bereinigen
        search_name = station_name or ''
        search_name = re.sub(
            r'\s*(inter\d+|mp3|aac|low|high|128|64|256).*$', '', search_name, flags=re.IGNORECASE
        )
        search_name = re.sub(r'\s*-\s*[A-Z]{2,3}\s*$', '', search_name)
        search_name = re.sub(r'\s*-\s*100%.*$', '', search_name, flags=re.IGNORECASE)
        search_name = re.sub(r'\s*91\.4.*$', '', search_name, flags=re.IGNORECASE)
        search_name = re.sub(r'\s*-\s*\d+\.\d+.*$', '', search_name)
        search_name = search_name.strip()

        if not search_name:
            return None, None, resolved_name, det_logo, search_logo

        log_debug(f"Suche radio.de API mit: '{search_name}' (Original: '{station_name}')")

        params = {'query': search_name, 'count': 20}
        response = api_client.get(RADIODE_SEARCH_API_URL, params=params, timeout=5)
        if response.status_code != 200:
            log_warning(f"radio.de API: ungueltige Antwort (Status {response.status_code})")
            return None, None, resolved_name, det_logo, search_logo

        data = response.json()
        log_debug(f"Search API: {data.get('totalCount', 0)} Treffer")

        if not ('playables' in data and len(data['playables']) > 0):
            log_debug(f"Keine Treffer fuer '{search_name}'")
            return None, None, resolved_name, det_logo, search_logo

        # Beste Uebereinstimmung aus den ersten 20 Treffern ermitteln
        best_match = None
        best_match_score = 0
        search_normalized = search_name.lower().replace('-', ' ').replace('_', ' ').strip()

        for station in data['playables'][:20]:
            station_found = station.get('name', '')
            station_normalized = station_found.lower().replace('-', ' ').replace('_', ' ').strip()

            # Exakter Match hat hoechste Prioritaet
            if station_normalized == search_normalized:
                best_match = station
                best_match_score = 1000
                log_debug(f"Exakter Match gefunden: '{station_found}'")
                break

            # Substring-Match
            if search_normalized in station_normalized:
                score = 100 + len(search_normalized)
                if score > best_match_score:
                    best_match = station
                    best_match_score = score
                    log_debug(f"Substring-Match: '{station_found}' - Score: {score}")

            # Wort-basierter Match
            elif search_normalized:
                search_words = set(search_normalized.split())
                station_words = set(station_normalized.split())
                matching_words = search_words.intersection(station_words)
                score = len(matching_words) * 10
                if score > best_match_score:
                    best_match = station
                    best_match_score = score
                    log_debug(
                        f"Wort-Match: '{station_found}' - Score: {score} (Woerter: {matching_words})"
                    )

        if not (best_match and best_match_score > 0):
            log_debug("Kein Match gefunden (Score zu niedrig)")
            return None, None, resolved_name, det_logo, search_logo

        station_found = best_match.get('name', '')
        station_id = best_match.get('id', '')
        found_logo = best_match.get('logo300x300', '')
        if found_logo:
            search_logo = found_logo
            log_info(f"Station-Logo aus Suche: {found_logo}")

        log_debug(
            f"Beste Uebereinstimmung: '{station_found}' (Score: {best_match_score}, ID: {station_id})"
        )

        if not station_id:
            log_debug("Keine Station-ID gefunden")
            return None, None, resolved_name, det_logo, search_logo

        # Now-Playing ueber Station-ID abrufen
        try:
            np_response = api_client.get(
                RADIODE_NOWPLAYING_API_URL, params={'stationIds': station_id}, timeout=5
            )
            if np_response.status_code == 200:
                np_data = np_response.json()
                if debug_log:
                    debug_log('radiode.now_playing.search', np_data)
                if isinstance(np_data, list) and len(np_data) > 0:
                    full_title = np_data[0].get('title', '')
                    log_debug(f"Empfangener Titel: '{full_title}'")
                    if full_title:
                        artist, title = parse_radiode_api_title(full_title, station_name)
                        if artist is not None or title is not None:
                            if artist and title:
                                log_info(f"OK now-playing API: {artist} - {title}")
                                return artist, title, resolved_name, det_logo, search_logo
                            if title:
                                log_info(f"OK now-playing API (nur Title): {title}")
                                return None, title, resolved_name, det_logo, search_logo
                    else:
                        log_debug(f"Titel-Format unbekannt: '{full_title}'")
                else:
                    log_debug("Leere now-playing Response")
            else:
                log_debug(f"now-playing API Fehler: {np_response.status_code}")
        except Exception as e:
            log_warning(f"Fehler bei now-playing API: {e}")

    except Exception as e:
        log_warning(f"Fehler bei radio.de API Abfrage: {e}")

    return None, None, resolved_name, det_logo, search_logo


__all__ = ['parse_radiode_api_title', 'get_nowplaying']
