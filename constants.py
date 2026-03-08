"""
Konstanten für das Audio Stream Monitor Addon.

Enthält API-Endpunkte, Header, Property-Namen, Timeouts und Regex-Patterns.
"""
import re
import xbmcaddon

# Addon-Informationen
ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo('id')
ADDON_NAME = ADDON.getAddonInfo('name')
ADDON_VERSION = ADDON.getAddonInfo('version')

# --- API Endpunkte ---

MUSICBRAINZ_API_URL = "https://musicbrainz.org/ws/2/recording/"
RADIODE_SEARCH_API_URL = "https://prod.radio-api.net/stations/search"
RADIODE_NOWPLAYING_API_URL = "https://api.radio.de/stations/now-playing"
RADIODE_DETAILS_API_URL = "https://prod.radio-api.net/stations/details"

# --- HTTP Headers ---

MUSICBRAINZ_HEADERS = {
    "User-Agent": f"RadioMonitorLight/{ADDON_VERSION} (https://github.com; Kodi addon {ADDON_ID})"
}
DEFAULT_HTTP_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
}

# --- Metadaten-Konstanten ---

INVALID_METADATA_VALUES = ['Unknown', 'Radio Stream', 'Internet Radio']

# --- Cache & Timeouts ---

MB_SONG_CACHE_TTL = 86400  # 24 Stunden

# Song-Timeout: Wie lange Properties nach dem letzten Titelwechsel behalten werden.
# Wenn MB eine Songlänge liefert, wird diese + BUFFER verwendet; sonst FALLBACK.
SONG_TIMEOUT_FALLBACK_S = 7 * 60  # Sekunden (Fallback wenn MB keine Länge liefert)
SONG_TIMEOUT_BUFFER_S = 90         # Sekunden Puffer nach erwartetem Songerende

# --- Window Property-Namen ---

class PropertyNames:
    """
    Window-Property-Namen als Konstanten.
    Verhindert Tippfehler und vereinfacht Umbenennung.
    """
    STATION    = 'RadioMonitor.Station'
    TITLE      = 'RadioMonitor.Title'
    ARTIST     = 'RadioMonitor.Artist'
    ALBUM      = 'RadioMonitor.Album'
    ALBUM_DATE = 'RadioMonitor.AlbumDate'
    GENRE      = 'RadioMonitor.Genre'
    MBID       = 'RadioMonitor.MBID'
    FIRST_REL  = 'RadioMonitor.FirstRelease'
    STREAM_TTL = 'RadioMonitor.StreamTitle'
    PLAYING    = 'RadioMonitor.Playing'
    LOGO       = 'RadioMonitor.Logo'
    BAND_FORM  = 'RadioMonitor.BandFormed'
    BAND_MEM   = 'RadioMonitor.BandMembers'

# Alias für Kompatibilität mit bestehendem Code
_P = PropertyNames

# --- Regex Patterns ---

# Regex für numerische IDs (z.B. "123 - 456") – an mehreren Stellen verwendet
NUMERIC_ID_PATTERN = re.compile(r'^\d+\s*-\s*\d+$')
