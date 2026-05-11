"""
Konstanten für das Audio Stream Monitor Addon.

Enthält API-Endpunkte, Header, Property-Namen, Timeouts und Regex-Patterns.
"""
import re
import xbmcaddon

# Addon-Informationen
try:
    ADDON = xbmcaddon.Addon()
except Exception:
    # Fallback fuer Aufrufe per direktem RunScript(Dateipfad,...),
    # bei denen Kodi keinen impliziten Addon-Kontext setzt.
    ADDON = xbmcaddon.Addon(id='service.audio.stream.monitor')
ADDON_ID = ADDON.getAddonInfo('id')
ADDON_NAME = ADDON.getAddonInfo('name')
ADDON_VERSION = ADDON.getAddonInfo('version')
QF_SERVICE_ADDON_ID = 'service.audio.stream.monitor.qf'

# --- API Endpunkte ---

MUSICBRAINZ_API_URL = "https://musicbrainz.org/ws/2/recording/"
MUSICBRAINZ_ARTIST_URL = "https://musicbrainz.org/ws/2/artist/"
RADIODE_SEARCH_API_URL = "https://prod.radio-api.net/stations/search"
RADIODE_NOWPLAYING_API_URL = "https://api.radio.de/stations/now-playing"
RADIODE_DETAILS_API_URL = "https://prod.radio-api.net/stations/details"
TUNEIN_DESCRIBE_API_URL = "https://opml.radiotime.com/Describe.ashx"
TUNEIN_TUNE_API_URL = "https://opml.radiotime.com/Tune.ashx"
TUNEIN_PARTNER_ID = "HyzqumNX"  # Feste Partner-ID des Kodi TuneIn-Addons

# --- HTTP Headers ---

MUSICBRAINZ_HEADERS = {
    "User-Agent": f"RadioMonitorLight/{ADDON_VERSION} (https://github.com; Kodi addon {ADDON_ID})"
}
DEFAULT_HTTP_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
}

# --- Metadaten-Konstanten ---

INVALID_METADATA_VALUES = ['Unknown', 'Radio Stream', 'Internet Radio']
STATION_NAME_MATCH_MIN_LEN = 5
RADIODE_PLUGIN_IDS = ('plugin.audio.radiode', 'plugin.audio.radio_de_light')
TUNEIN_PLUGIN_IDS = ('plugin.audio.tunein2017',)

# MB-Kandidat-Auswahl
MB_WINNER_MIN_SCORE = 60
MB_WINNER_MIN_COMBINED = 55.0
# MB-Bereinigung: Mindest-Aehnlichkeit damit MB den Artist/Title-Label korrigieren darf.
# Unter diesem Schwellwert wird der Originalwert aus der Quelle behalten.
MB_LABEL_CORRECTION_MIN_SIM = 0.85
MP_TRUST_MAX_MISMATCHES = 2
# Feature-Flag: MusicPlayer als eigenstaendige Songquelle (parallel zu API/ICY).
# Bewusst deaktiviert – MusicPlayer-Daten werden bereits als Vergleichsquelle
# in der Source-Policy beruecksichtigt. Eine separate Entscheidungsschicht
# wuerde die Logik unnoetig verdoppeln. Auf True setzen um den Pfad zu reaktivieren.
MP_DECISION_ENABLED = False

# --- Quellen-Familien ---

SOURCE_FAMILIES = ('asm-qf', 'musicplayer', 'api', 'icy')
STREAM_SOURCE_FAMILIES = ('asm-qf', 'api', 'icy')  # Subset: externe Stream-Quellen ohne MusicPlayer
ICY_FORMAT_KEYS = ('artist_title', 'title_artist', 'unknown')

# Trigger-Bezeichner fuer Quellenwechsel
TRIGGER_TITLE_CHANGE = 'Titelwechsel'
TRIGGER_QF_CHANGE = 'ASM-QF-Wechsel'
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
SONG_TIMEOUT_EARLY_CLEAR_S = 20
SONG_TIMEOUT_FALLBACK_S = 4 * 60
# MB-Laengen unterhalb dieser Schwelle gelten nur fuer den Timeout als unplausibel
# (haeufig Edit/Jingle/Fehlmatch-Varianten) und triggern Fallback statt Early-Clear.
MB_TIMEOUT_MIN_DURATION_MS = 120 * 1000
API_NOW_REFRESH_INTERVAL_S = 10
# QF-Dominanz: Solange innerhalb dieses Fensters keine frische QF-Response vorliegt,
# bleibt QF weiterhin fuehrend (kein Fallback auf API/ICY/MP).
# Erst danach darf bei QF-Ausfall auf andere Quellen zurueckgegriffen werden.
QF_NO_RESPONSE_FALLBACK_S = 25.0
# Nach dieser Zeit mit frischen externen QF-`error`-Responses wird QF ebenfalls
# als nicht verfuegbar behandelt und faellt auf die Standard-Quellengruppe zurueck.
QF_ERROR_FALLBACK_S = 30.0
# Nach dieser Zeit ohne echte QF-Antwort wird QF in den Degrade-Modus versetzt.
# In diesem Modus faellt ASM auf Standardquellen zurueck.
QF_NO_RESPONSE_DEGRADE_S = 10 * 60.0
# Im QF-Degrade-Modus wird nur noch periodisch ein Probe-Request gesendet.
QF_DEGRADE_PROBE_INTERVAL_S = 3 * 60.0
# Kurzes Grace-Fenster fuer non-fresh QF-Hits bei Request-ID-Race.
# Nur innerhalb dieses Fensters duerfen hit-Paare trotz id_mismatch als nutzbar gelten.
QF_HIT_MISMATCH_GRACE_S = 3.0
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
STATION_PROFILE_MIN_SESSION_S = 10 * 60
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
# Persistente Quellen-Statistik (pro Einzelquelle inkl. ASM-QF)
SOURCE_STATS_FAMILIES = ('api', 'icy', 'musicplayer', 'asm-qf')
# Persistente Quellgruppen-Hints fuer Source-Policy (bewusst nur api/icy/musicplayer)
SOURCE_GROUP_FAMILIES = ('api', 'icy', 'musicplayer')
SOURCE_GROUP_DB_MIN_SAMPLES = 8
SOURCE_GROUP_DB_MIN_SHARE = 0.55
SOURCE_GROUP_DB_SWAP_MIN_SAMPLES = 6
SOURCE_GROUP_DB_SWAP_MIN_SHARE = 0.60
# Source policy defaults
SOURCE_POLICY_WINDOW = 40
SOURCE_POLICY_SWITCH_MARGIN = 0.12
SOURCE_POLICY_SINGLE_CONFIRM_POLLS = 2
# Startup source qualification window (after buffering settled)
STARTUP_SOURCE_QUALIFY_WINDOW_S = 8.0
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
    STATION        = 'RadioMonitor.Station'
    TITLE          = 'RadioMonitor.Title'
    ARTIST         = 'RadioMonitor.Artist'
    ARTIST_DISPLAY = 'RadioMonitor.ArtistDisplay'
    ALBUM          = 'RadioMonitor.Album'
    ALBUM_DATE = 'RadioMonitor.AlbumDate'
    GENRE      = 'RadioMonitor.Genre'
    MBID       = 'RadioMonitor.ArtistMBID'
    FIRST_REL  = 'RadioMonitor.FirstRelease'
    STREAM_TTL = 'RadioMonitor.StreamTitle'
    API_NOW    = 'RadioMonitor.ApiNowPlaying'
    ICY_NOW    = 'RadioMonitor.IcyNowPlaying'
    SOURCE     = 'RadioMonitor.Source'
    SOURCE_DETAIL = 'RadioMonitor.SourceDetail'
    SOURCE_SWAP_STATUS = 'RadioMonitor.SourceSwapStatus'
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
    VERIFIED_SOURCE_URL = 'RadioMonitor.VerifiedSourceUrl'
    VERIFIED_SOURCE_BY = 'RadioMonitor.VerifiedSourceBy'
    VERIFIED_SOURCE_CONF = 'RadioMonitor.VerifiedSourceConfidence'
    QF_REQUEST_ID = 'RadioMonitor.QF.Request.Id'
    QF_REQUEST_STATION = 'RadioMonitor.QF.Request.Station'
    QF_REQUEST_STATION_ID = 'RadioMonitor.QF.Request.StationId'
    QF_REQUEST_MODE = 'RadioMonitor.QF.Request.Mode'
    QF_REQUEST_TS = 'RadioMonitor.QF.Request.Ts'
    QF_RESPONSE_ID = 'RadioMonitor.QF.Response.Id'
    QF_RESPONSE_STATUS = 'RadioMonitor.QF.Response.Status'
    QF_RESPONSE_ARTIST = 'RadioMonitor.QF.Response.Artist'
    QF_RESPONSE_TITLE = 'RadioMonitor.QF.Response.Title'
    QF_RESPONSE_SOURCE = 'RadioMonitor.QF.Response.Source'
    QF_RESPONSE_REASON = 'RadioMonitor.QF.Response.Reason'
    QF_RESPONSE_META = 'RadioMonitor.QF.Response.Meta'
    QF_RESPONSE_STATION_USED = 'RadioMonitor.QF.Response.StationUsed'
    QF_RESPONSE_TS = 'RadioMonitor.QF.Response.Ts'
    QF_RESULT = 'RadioMonitor.QF.Result'
    QF_ENABLED = 'RadioMonitor.QF.Enabled'
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
# Re-Count-Schutz: gleicher Song pro Sender wird innerhalb dieses Fensters nicht erneut gezählt.
SONG_RECOUNT_WINDOW_S = 10 * 60

# Song-Historie (Settings-Ansicht)
SONG_HISTORY_STATION_LIMIT = 80
SONG_HISTORY_SONG_LIMIT = 250
SONG_HISTORY_WINDOW_XML = 'song_history_window.xml'
SONG_HISTORY_WINDOW_SKIN = 'default'
SONG_HISTORY_WINDOW_RESOLUTION = '1080i'

# Song-Historie (WindowXML IDs + Actions)
SONG_HISTORY_ACTION_PREVIOUS_MENU = 10
SONG_HISTORY_ACTION_PARENT_DIR = 9
SONG_HISTORY_ACTION_NAV_BACK = 92
SONG_HISTORY_CTRL_CLOSE_BUTTON = 100
SONG_HISTORY_CTRL_STATION_BUTTON = 110
SONG_HISTORY_CTRL_STATION_MENU = 111
SONG_HISTORY_CTRL_SONG_LIST = 120
SONG_HISTORY_CTRL_SUMMARY_LABEL = 130

# --- Regex Patterns ---

# Regex für numerische IDs (z.B. "123 - 456") – an mehreren Stellen verwendet
NUMERIC_ID_PATTERN = re.compile(r'^\d+\s*-\s*\d+$')
