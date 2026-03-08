"""
radio.de API - Hilfsfunktionen.

Exportiert Parsing-Funktionen für radio.de API-Antworten.
"""
from constants import INVALID_METADATA_VALUES, NUMERIC_ID_PATTERN as _NUMERIC_ID_RE


def parse_radiode_api_title(full_title, station_name=None):
    """
    Parst radio.de API Format "ARTIST - TITLE". Gibt (artist, title) zurück;
    ungültige Werte werden zu ''/None. station_name wird als ungültiger Title gefiltert.
    """
    invalid = INVALID_METADATA_VALUES + ['']
    if not full_title or ' - ' not in full_title:
        return None, None
    parts = full_title.split(' - ', 1)
    artist = parts[0].strip()
    title = parts[1].strip()
    if artist in invalid:
        artist = ''
    if title in invalid or (station_name and title == station_name):
        title = ''
    if title and _NUMERIC_ID_RE.match(title):
        return None, None
    return artist or None, title or None


__all__ = ['parse_radiode_api_title']
