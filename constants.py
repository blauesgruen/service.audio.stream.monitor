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
RADIODE_PLUGIN_IDS = ('plugin.audio.radiode', 'plugin.audio.radio_de_light')
TUNEIN_PLUGIN_IDS = ('plugin.audio.tunein2017',)

# MB-Kandidat-Auswahl
MB_WINNER_MIN_SCORE = 60
MB_WINNER_MIN_COMBINED = 55.0
# MB-Bereinigung: Mindest-Aehnlichkeit damit MB den Artist/Title-Label korrigieren darf.
# Unter diesem Schwellwert wird der Originalwert aus der Quelle behalten.
MB_LABEL_CORRECTION_MIN_SIM = 0.85
MP_TRUST_MAX_MISMATCHES = 2
MP_DECISION_ENABLED = False

# Trigger-Bezeichner fuer Quellenwechsel
TRIGGER_TITLE_CHANGE = 'Titelwechsel'
TRIGGER_API_CHANGE = 'API-Wechsel'
TRIGGER_MP_CHANGE = 'MusicPlayer-Wechsel'
TRIGGER_MP_INVALID = 'MusicPlayer ungueltig'
TRIGGER_ICY_STALE = 'ICY-Wechsel (API stale)'

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
API_NOW_REFRESH_INTERVAL_S = 10
# Song-Ende-Detektor (fruehes Leeren vor dem harten Timeout).
# Keywords sind nur Hinweise und loesen nie allein aus.
SONG_END_DETECTOR_ENABLED = True
SONG_END_MIN_SONG_AGE_S = 45.0
SONG_END_HOLD_S = 8.0
SONG_END_MIN_KEYWORD_HITS = 2
SONG_END_MIN_NON_SONG_SOURCES = 2
SONG_END_REQUIRE_ADDITIONAL_SIGNAL = True
SONG_END_STALE_API_MIN_S = 12.0
SONG_END_NEAR_TIMEOUT_S = 30.0
# Rueckwaertskompatibilitaet (legacy name)
API_DATA_REFRESH_INTERVAL_S = API_NOW_REFRESH_INTERVAL_S
API_METADATA_POLL_INTERVAL_S = 10
MUSICPLAYER_FALLBACK_POLL_INTERVAL_S = 5
# Analysis event persistence
ANALYSIS_ENABLED = True
ANALYSIS_EVENTS_FILENAME = 'analysis_events.jsonl'
ANALYSIS_MAX_EVENTS = 1500
ANALYSIS_FLUSH_INTERVAL_S = 5.0
# Station profile learning
STATION_PROFILE_DIRNAME = 'station_profiles'
STATION_PROFILE_FILENAME = 'station_profiles.json'  # legacy aggregate filename (migration)
SONG_DB_FILENAME = 'song_data.db'
STATION_PROFILE_ALPHA = 0.30
STATION_PROFILE_MIN_SESSION_S = 15 * 60
STATION_PROFILE_MIN_STABLE_SESSIONS = 2
STATION_PROFILE_CONFIDENCE_LOW = 0.20
STATION_PROFILE_CONFIDENCE_HIGH = 0.60
STATION_PROFILE_OBSERVE_INTERVAL_S = 5.0
STATION_PROFILE_SAVE_INTERVAL_S = 30.0
STATION_PROFILE_ICY_STRUCTURAL_GENERIC_THRESHOLD = 0.90
STATION_PROFILE_MP_ABSENT_SONG_RATE_MAX = 0.05
STATION_PROFILE_MP_NOISE_FLIP_RATE_MIN = 0.30
STATION_PROFILE_MP_NOISE_RELIABLE_EMA_MAX = 0.25
STATION_PROFILE_KEYWORD_STATS_MAX = 40
# Source policy defaults
SOURCE_POLICY_WINDOW = 40
SOURCE_POLICY_SWITCH_MARGIN = 0.12
SOURCE_POLICY_SINGLE_CONFIRM_POLLS = 2
# Startup source qualification window (after buffering settled)
STARTUP_SOURCE_QUALIFY_WINDOW_S = 20.0
STARTUP_API_ONLY_STABLE_POLLS = 3
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
    API_NOW    = 'RadioMonitor.ApiNowPlaying'
    ICY_NOW    = 'RadioMonitor.IcyNowPlaying'
    SOURCE     = 'RadioMonitor.Source'
    SOURCE_DETAIL = 'RadioMonitor.SourceDetail'
    PLAYING    = 'RadioMonitor.Playing'
    LOGO       = 'RadioMonitor.Logo'
    BAND_FORM  = 'RadioMonitor.BandFormed'
    BAND_MEM   = 'RadioMonitor.BandMembers'
    MB_DUR_MS  = 'RadioMonitor.MBDurationMs'
    MB_DUR_S   = 'RadioMonitor.MBDurationS'
    TMO_TOTAL  = 'RadioMonitor.TimeoutTotal'
    TMO_LEFT   = 'RadioMonitor.TimeoutRemaining'
    RADIODE_LOGO = 'RadioDE.StationLogo'
    RADIODE_NAME = 'RadioDE.StationName'
    # Raw source labels (one label per raw source)
    RAW_STREAM_URL = 'RadioMonitor.Source.StreamUrl'
    RAW_PLUGIN_URL = 'RadioMonitor.Source.PluginPlaybackUrl'
    RAW_STREAM_HEADERS = 'RadioMonitor.Source.StreamHeaders'
    RAW_ICY_METADATA = 'RadioMonitor.Source.IcyMetadataRaw'
    RAW_ICY_STREAMTITLE = 'RadioMonitor.Source.IcyStreamTitleRaw'
    RAW_ICY_PARSED = 'RadioMonitor.Source.IcyParsed'
    RAW_PLAYING_ITEM = 'RadioMonitor.Source.PlayingItemRaw'
    RAW_JSONRPC_PLAYER = 'RadioMonitor.Source.JsonRpcPlayerRaw'
    RAW_LISTITEM = 'RadioMonitor.Source.ListItemRaw'
    RAW_API_RADIODE_NOWPLAYING = 'RadioMonitor.Source.Api.RadioDeNowPlayingRaw'
    RAW_API_TUNEIN_JSON = 'RadioMonitor.Source.Api.TuneInJsonRaw'
    RAW_API_TUNEIN_TEXT = 'RadioMonitor.Source.Api.TuneInTextRaw'
    AN_TRACE_ID = 'RadioMonitor.Analysis.TraceId'
    AN_TRIGGER = 'RadioMonitor.Analysis.Trigger'
    AN_WINNER_SOURCE = 'RadioMonitor.Analysis.WinnerSource'
    AN_WINNER_PAIR = 'RadioMonitor.Analysis.WinnerPair'
    AN_LAST_EVENT = 'RadioMonitor.Analysis.LastEvent'

# Alias für Kompatibilität mit bestehendem Code
_P = PropertyNames

# --- Keyword-Lernen ---

# Mindestlänge eines Strings, um als Generic-String-Kandidat zu gelten
GENERIC_STRING_MIN_LEN = 8
# Maximale Anzahl aufeinanderfolgender Ziffern in einem Kandidaten-String
# Strings mit mehr Ziffern am Stück (Telefonnummern, Frequenzen) werden verworfen
GENERIC_STRING_MAX_DIGIT_SEQ = 3
# Mindestanzahl Beobachtungen, bevor ein Generic-String als Keyword promoted wird
KEYWORD_PROMOTE_MIN_SEEN = 5
# Maximale Anzahl bestätigter Songs im LRU-Cache pro Sender
SONG_CACHE_MAX_PER_STATION = 200

# --- Regex Patterns ---

# Regex für numerische IDs (z.B. "123 - 456") – an mehreren Stellen verwendet
NUMERIC_ID_PATTERN = re.compile(r'^\d+\s*-\s*\d+$')
