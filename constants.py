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
TUNEIN_DESCRIBE_API_URL = "https://opml.radiotime.com/Describe.ashx"
TUNEIN_TUNE_API_URL = "https://opml.radiotime.com/Tune.ashx"

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

# MusicBrainz Work-Kontext: harte Laufzeit- und Lookup-Grenzen fuer
# reaktionsschnelles Live-Monitoring.
MB_WORK_CONTEXT_ENABLED = True
MB_WORK_CONTEXT_MAX_SECONDS = 3.0
MB_WORK_CONTEXT_MAX_PAGES = 1
MB_WORK_CONTEXT_MAX_DETAIL_LOOKUPS = 2
MB_WORK_CONTEXT_RATE_LIMIT_S = 1.0

# Song-Timeout: Wie lange Properties nach dem letzten Titelwechsel behalten werden.
# Wenn MB eine Songlaenge liefert, wird SONG_TIMEOUT_EARLY_CLEAR_S abgezogen.
# Wenn keine MB-Laenge bekannt ist, greift SONG_TIMEOUT_FALLBACK_S.
SONG_TIMEOUT_EARLY_CLEAR_S = 15
SONG_TIMEOUT_FALLBACK_S = 4 * 60
API_DATA_REFRESH_INTERVAL_S = 10
# Nach Ende des Kodi-Bufferings kurz stabilen Zustand abwarten,
# bevor die Quellenentscheidung startet.
PLAYER_BUFFER_SETTLE_S = 2.0
# Safety-Net: falls Kodi den Buffer-Status nicht sauber meldet, wird die
# Quellenfestlegung nach dieser Zeit trotzdem gestartet.
PLAYER_BUFFER_MAX_WAIT_S = 45.0

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
    API_DATA   = 'RadioMonitor.ApiData'
    SOURCE     = 'RadioMonitor.Source'
    PLAYING    = 'RadioMonitor.Playing'
    LOGO       = 'RadioMonitor.Logo'
    BAND_FORM  = 'RadioMonitor.BandFormed'
    BAND_MEM   = 'RadioMonitor.BandMembers'
    MB_DUR_MS  = 'RadioMonitor.MBDurationMs'
    MB_DUR_S   = 'RadioMonitor.MBDurationS'
    TMO_TOTAL  = 'RadioMonitor.TimeoutTotal'
    TMO_LEFT   = 'RadioMonitor.TimeoutRemaining'

# Alias für Kompatibilität mit bestehendem Code
_P = PropertyNames

# --- Regex Patterns ---

# Regex für numerische IDs (z.B. "123 - 456") – an mehreren Stellen verwendet
NUMERIC_ID_PATTERN = re.compile(r'^\d+\s*-\s*\d+$')
