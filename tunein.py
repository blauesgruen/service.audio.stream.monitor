"""
TuneIn API - Hilfsfunktionen.

Exportiert Parsing- und Abfragefunktionen fuer TuneIn-Streams.
"""
import re
from urllib.parse import unquote
from constants import (
    INVALID_METADATA_VALUES,
    NUMERIC_ID_PATTERN as _NUMERIC_ID_RE,
    TUNEIN_DESCRIBE_API_URL,
    TUNEIN_TUNE_API_URL,
)
from metadata import parse_stream_title_simple as _parse_stream_title_simple
from logger import log_debug, log_info


def extract_station_id(text):
    """
    Extrahiert eine TuneIn-ID (z.B. s24878, t109814382) aus Plugin- oder Stream-URLs.
    Unterstuetzt auch verschachtelt URL-encodete fparams.
    """
    if not text:
        return None

    try:
        decoded = str(text)
        for _ in range(3):
            new_decoded = unquote(decoded)
            if new_decoded == decoded:
                break
            decoded = new_decoded

        patterns = [
            r'[?&](?:sid|preset_id|id|stationId)=([sptufl]\d+(?:-\d+)?)',
            r'["\'](?:sid|preset_id|id|stationId)["\']\s*:\s*["\']([sptufl]\d+(?:-\d+)?)["\']',
            r'/([sptufl]\d+(?:-\d+)?)(?:[/?&]|$)',
        ]

        for pattern in patterns:
            match = re.search(pattern, decoded, re.IGNORECASE)
            if not match:
                continue
            candidate = (match.group(1) or '').strip()
            if '-' in candidate:
                candidate = candidate.split('-', 1)[0]
            if re.match(r'^[sptufl]\d+$', candidate, re.IGNORECASE):
                return candidate
    except Exception:
        pass
    return None


def parse_nowplaying_candidate(value, station_name=None):
    """Parst einen potenziellen TuneIn Now-Playing-String zu (artist, title)."""
    if value is None:
        return None, None

    candidate = str(value).strip()
    if not candidate:
        return None, None
    if candidate.lower().startswith('http'):
        return None, None

    invalid = INVALID_METADATA_VALUES + ['']
    if station_name:
        invalid.append(station_name)
        invalid.append(station_name.lower())

    if candidate in invalid or candidate.lower() in invalid:
        return None, None

    # "Song: Artist - Title" -> Prefix entfernen
    candidate = re.sub(r'^\s*Song:\s*', '', candidate, flags=re.IGNORECASE).strip()
    if not candidate:
        return None, None

    # Numerische IDs konsequent ignorieren
    if _NUMERIC_ID_RE.match(candidate):
        return None, None

    if ' - ' in candidate:
        artist, title = _parse_stream_title_simple(candidate)
        if title and _NUMERIC_ID_RE.match(title):
            return None, None
        if artist and re.match(r'^\d+$', artist):
            artist = None
        if title:
            return artist, title

    # Fallback: ungetrennter Kandidat als Title
    if not _NUMERIC_ID_RE.match(candidate):
        return None, candidate
    return None, None


def extract_from_json(payload, station_name=None):
    """Durchsucht JSON rekursiv nach Now-Playing-Kandidaten."""
    candidates = []
    preferred_keys = {
        'playing', 'song', 'subtitle', 'subtext', 'now_playing', 'nowplaying',
        'current_song', 'currentsong', 'current_track', 'title', 'text'
    }

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                k = str(key).lower()
                if k in preferred_keys and isinstance(value, (str, int, float)):
                    candidates.append(str(value))
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)

    best_artist, best_title = None, None
    for candidate in candidates:
        artist, title = parse_nowplaying_candidate(candidate, station_name)
        if artist and title:
            return artist, title  # vollstaendiges Paar gefunden
        if (artist or title) and not best_title:
            best_artist, best_title = artist, title  # erstes Teilresultat merken
    return best_artist, best_title


def extract_from_text(text, station_name=None):
    """Fallback-Parser fuer XML/Plain-Text Antworten aus TuneIn OPML APIs."""
    if not text:
        return None, None

    patterns = [
        r'playing="([^"]+)"',
        r'subtext="([^"]+)"',
        r'"playing"\s*:\s*"([^"]+)"',
        r'"subtitle"\s*:\s*"([^"]+)"',
        r'"subtext"\s*:\s*"([^"]+)"',
    ]

    best_artist, best_title = None, None
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            candidate = match.group(1).strip()
            artist, title = parse_nowplaying_candidate(candidate, station_name)
            if artist and title:
                return artist, title  # vollstaendiges Paar gefunden
            if (artist or title) and not best_title:
                best_artist, best_title = artist, title
    return best_artist, best_title


def get_nowplaying(api_client, station_id, station_name=None, debug_log=None):
    """
    Holt aktuelle Song-Info fuer TuneIn-Streams ueber OPML-Endpunkte.

    debug_log: optionales callable(context, payload) fuer Debug-Ausgaben
    """
    if not station_id:
        return None, None

    endpoints = [
        (TUNEIN_DESCRIBE_API_URL, {'id': station_id, 'render': 'json'}),
        (TUNEIN_TUNE_API_URL, {'id': station_id, 'render': 'json'}),
        (TUNEIN_TUNE_API_URL, {'id': station_id, 'render': 'json', 'formats': 'mp3,aac,ogg,hls'}),
    ]

    for i, (endpoint, params) in enumerate(endpoints):
        try:
            response = api_client.get(endpoint, params=params, timeout=5)
            if response.status_code != 200:
                log_debug(f"TuneIn API Status {response.status_code} fuer {endpoint}")
                continue

            try:
                payload = response.json()
            except Exception:
                payload = None

            if payload is not None:
                # Describe-Endpunkt (i==0): has_song=False bedeutet keine Song-Daten vorhanden.
                # Tune-Endpunkte liefern nur Stream-URLs, nie Song-Metadaten – fruehzeitig abbrechen.
                if i == 0:
                    body = payload.get('body', []) if isinstance(payload, dict) else []
                    station = body[0] if (isinstance(body, list) and body and isinstance(body[0], dict)) else {}
                    if station.get('element') == 'station' and station.get('has_song') is False:
                        return None, None

                if debug_log:
                    debug_log('tunein.json', payload)

                artist, title = extract_from_json(payload, station_name)
                if artist or title:
                    log_info(f"OK TuneIn API: {artist} - {title}")
                    return artist, title

            if debug_log:
                debug_log('tunein.text', response.text)
            artist, title = extract_from_text(response.text, station_name)
            if artist or title:
                log_info(f"OK TuneIn API (Text): {artist} - {title}")
                return artist, title
        except Exception as e:
            log_debug(f"Fehler bei TuneIn API Abfrage ({endpoint}): {e}")

    return None, None


__all__ = [
    'extract_station_id',
    'parse_nowplaying_candidate',
    'extract_from_json',
    'extract_from_text',
    'get_nowplaying',
]
