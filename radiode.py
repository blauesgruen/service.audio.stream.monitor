"""
radio.de API - Hilfsfunktionen.

Exportiert Parsing-Funktionen für radio.de API-Antworten.
"""
import re
from constants import INVALID_METADATA_VALUES, NUMERIC_ID_PATTERN as _NUMERIC_ID_RE


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


__all__ = ['parse_radiode_api_title']
