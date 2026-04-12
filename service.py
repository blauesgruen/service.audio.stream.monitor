import xbmc
import xbmcgui
import re
import time
import threading
import json
import os
from urllib.parse import urlparse, parse_qs, unquote
try:
    import xbmcvfs
except Exception:  # pragma: no cover - Kodi runtime dependency
    xbmcvfs = None

# --- Modul-Imports ---
from constants import (
    ADDON, ADDON_ID, QF_SERVICE_ADDON_ID as _QF_SERVICE_ADDON_ID,
    RADIODE_SEARCH_API_URL,
    DEFAULT_HTTP_HEADERS, INVALID_METADATA_VALUES, STATION_NAME_MATCH_MIN_LEN,
    SONG_TIMEOUT_FALLBACK_S, SONG_TIMEOUT_EARLY_CLEAR_S,
    SONG_END_DETECTOR_ENABLED, SONG_END_MIN_SONG_AGE_S, SONG_END_HOLD_S,
    SONG_END_MIN_KEYWORD_HITS, SONG_END_MIN_NON_SONG_SOURCES,
    SONG_END_REQUIRE_ADDITIONAL_SIGNAL, SONG_END_STALE_API_MIN_S,
    SONG_END_NEAR_TIMEOUT_S,
    API_NOW_REFRESH_INTERVAL_S, QF_NO_RESPONSE_FALLBACK_S, PLAYER_BUFFER_SETTLE_S, PLAYER_BUFFER_MAX_WAIT_S,
    API_METADATA_POLL_INTERVAL_S, MUSICPLAYER_FALLBACK_POLL_INTERVAL_S,
    ANALYSIS_ENABLED, ANALYSIS_EVENTS_FILENAME, ANALYSIS_MAX_EVENTS, ANALYSIS_FLUSH_INTERVAL_S,
    STATION_PROFILE_DIRNAME, STATION_PROFILE_OBSERVE_INTERVAL_S, STATION_PROFILE_SAVE_INTERVAL_S,
    SOURCE_POLICY_WINDOW, SOURCE_POLICY_SWITCH_MARGIN, SOURCE_POLICY_SINGLE_CONFIRM_POLLS,
    STARTUP_SOURCE_QUALIFY_WINDOW_S, STARTUP_API_ONLY_STABLE_POLLS,
    STREAM_SOURCE_FAMILIES as _STREAM_SOURCE_FAMILIES,
    RADIODE_PLUGIN_IDS as _RADIODE_PLUGIN_IDS,
    TUNEIN_PLUGIN_IDS as _TUNEIN_PLUGIN_IDS,
    MB_WINNER_MIN_SCORE as _MB_WINNER_MIN_SCORE,
    MB_WINNER_MIN_COMBINED as _MB_WINNER_MIN_COMBINED,
    MB_LABEL_CORRECTION_MIN_SIM as _MB_LABEL_CORRECTION_MIN_SIM,
    MP_TRUST_MAX_MISMATCHES as _MP_TRUST_MAX_MISMATCHES,
    MP_DECISION_ENABLED as _MP_DECISION_ENABLED,
    TRIGGER_TITLE_CHANGE as _TRIGGER_TITLE_CHANGE,
    TRIGGER_QF_CHANGE as _TRIGGER_QF_CHANGE,
    TRIGGER_API_CHANGE as _TRIGGER_API_CHANGE,
    TRIGGER_MP_CHANGE as _TRIGGER_MP_CHANGE,
    TRIGGER_MP_INVALID as _TRIGGER_MP_INVALID,
    TRIGGER_ICY_STALE as _TRIGGER_ICY_STALE,
    PropertyNames as _P, NUMERIC_ID_PATTERN as _NUMERIC_ID_RE
)
from logger import log_debug, log_info, log_warning, log_error
from api_client import APIClient
from source_policy import SourcePolicy
from station_profiles import StationProfileStore
from startup_qualifier import StartupQualifier
from musicplayer_trust import MusicPlayerTrust
from song_end_detector import SongEndDetector
from raw_sources import RawSourceLabels, snapshot_getters
from analysis_events import AnalysisEventStore, new_trace_id
import skin_colors as _skin_colors
from musicbrainz import (
    identify_artist_title_via_musicbrainz as _identify_artist_title_via_musicbrainz,
    musicbrainz_query_artist_info as _musicbrainz_query_artist_info,
    musicbrainz_query_recording as _musicbrainz_query_recording,
    mb_similarity as _mb_similarity,
    _mb_cache,
)
from radiode import get_nowplaying as _radiode_get_nowplaying
from tunein import (
    extract_station_id as _tunein_extract_station_id,
    get_nowplaying as _tunein_get_nowplaying,
)
from metadata import (
    extract_stream_title as _extract_stream_title,
    parse_stream_title_simple as _parse_stream_title_simple,
    parse_stream_title_complex as _parse_metadata_complex,
    get_last_separator_variant as _get_last_separator_variant,
    is_song_pair as _is_song_pair,
    is_generic_metadata_text as _is_generic_metadata_text,
    is_generic_song_pair as _is_generic_song_pair,
    has_non_generic_song_pair as _has_non_generic_song_pair,
    filter_non_generic_song_pairs as _filter_non_generic_song_pairs,
)
from raw_candidate_extractors import (
    extract_listitem_pair as _extract_listitem_pair,
    extract_playing_item_pair as _extract_playing_item_pair,
    extract_jsonrpc_pair as _extract_jsonrpc_pair,
)
from pre_mb_policy import (
    normalize_station_compare_text as _normalize_station_compare_text,
    build_station_hints as _build_station_hints,
    is_station_name_match_pair as _is_station_name_match_pair,
    is_obvious_non_song_text as _pre_mb_is_obvious_non_song_text,
    sanitize_pair_for_pre_mb as _sanitize_pair_for_pre_mb,
    is_pre_mb_plausible_pair as _is_pre_mb_plausible_pair,
)


# Window-Properties für die Skin
WINDOW = xbmcgui.Window(10000)  # Home window

class PlayerMonitor(xbmc.Player):
    """Monitor für Player-Events um Logo SOFORT beim Stream-Start zu erfassen"""
    def __init__(self, radio_monitor):
        super(PlayerMonitor, self).__init__()
        self.radio_monitor = radio_monitor
    
    def onPlayBackStarted(self):
        """Liest Plugin-Slug aus radio.de light URL – vor Stream-Auflösung verfügbar"""
        self.radio_monitor._reset_api_context()
        try:
            playing_file = self.getPlayingFile()
            self.radio_monitor._capture_plugin_playback_raw(playing_file)
            self.radio_monitor._capture_listitem_raw('onPlayBackStarted')
            if (
                self.radio_monitor.current_url
                and playing_file
                and playing_file != self.radio_monitor.current_url
            ):
                self.radio_monitor._handle_stream_transition(
                    f"onPlayBackStarted: URL-Wechsel erkannt ({self.radio_monitor.current_url} -> {playing_file})"
                )
            if 'plugin.audio.radio_de_light' in playing_file:
                self.radio_monitor._set_api_source(self.radio_monitor.API_SOURCE_RADIODE)
                parsed = urlparse(playing_file)
                params = parse_qs(parsed.query)
                iconimage_list = params.get('iconimage', [])
                if iconimage_list:
                    iconimage = unquote(iconimage_list[0])
                    slug_match = re.search(r'/([^/]+)\.[a-z]+(?:\?|$)', iconimage)
                    if slug_match:
                        slug = slug_match.group(1)
                        self.radio_monitor.plugin_slug = slug
                        if not self.radio_monitor.station_id:
                            self.radio_monitor.station_id = slug
                        log_info(f"Plugin-Slug aus iconimage: '{slug}'")
            elif 'plugin.audio.radiode' in playing_file:
                self.radio_monitor._set_api_source(self.radio_monitor.API_SOURCE_RADIODE)
                log_debug("radio.de Addon erkannt (plugin.audio.radiode)")
            elif 'plugin.audio.tunein2017' in playing_file:
                self.radio_monitor._set_api_source(self.radio_monitor.API_SOURCE_TUNEIN)
                tunein_id = _tunein_extract_station_id(playing_file)
                if tunein_id:
                    self.radio_monitor.tunein_station_id = tunein_id
                    log_info(f"TuneIn Station-ID aus Plugin-URL: '{tunein_id}'")

            # Fallback: Addon-Plugin-ID ist nicht immer im aufgeloesten PlayingFile enthalten.
            # Dann Quelle aus URL-Hints (z.B. aggregator=tunein / aggregator=radio-de) ableiten.
            self.radio_monitor._ensure_api_source_from_context(playing_file, 'onPlayBackStarted')
            self.radio_monitor._reconcile_api_source('onPlayBackStarted')
        except Exception as e:
            log_debug(f"Fehler in onPlayBackStarted: {e}")

    def onPlayBackStopped(self):
        try:
            self.radio_monitor._handle_playback_stop("onPlayBackStopped")
        except Exception as e:
            log_debug(f"Fehler in onPlayBackStopped: {e}")

    def onPlayBackEnded(self):
        try:
            self.radio_monitor._handle_playback_stop("onPlayBackEnded")
        except Exception as e:
            log_debug(f"Fehler in onPlayBackEnded: {e}")

    def onAVStarted(self):
        """Wird aufgerufen SOFORT wenn Stream startet - ListItem.Icon ist noch verfügbar!"""
        try:
            if self.isPlayingVideo():
                # Video gestartet → Radio-Properties sofort löschen
                log_info("Video gestartet - lösche Radio-Properties sofort")
                self.radio_monitor.is_playing = False
                self.radio_monitor.current_url = None
                self.radio_monitor.stop_metadata_monitoring()
                self.radio_monitor.clear_properties()
                return

            if self.isPlayingAudio():
                playing_file = self.getPlayingFile()

                # Lokale Datei → Radio-Properties sofort löschen
                if not (playing_file.startswith('http://') or playing_file.startswith('https://')):
                    log_info("Lokale Datei gestartet - lösche Radio-Properties sofort")
                    self.radio_monitor.is_playing = False
                    self.radio_monitor.current_url = None
                    self.radio_monitor.stop_metadata_monitoring()
                    self.radio_monitor.clear_properties()
                    return

                # HTTP/HTTPS Audio-Stream → SOFORT Logo vom ListItem lesen
                listitem_icon = xbmc.getInfoLabel('ListItem.Icon')
                if listitem_icon and self.radio_monitor.is_real_logo(listitem_icon):
                    self.radio_monitor.station_logo = listitem_icon
                    log_info(f"Logo SOFORT beim Start erfasst: {listitem_icon}")
                else:
                    log_debug(f"ListItem.Icon beim Start: {listitem_icon}")
        except Exception as e:
            log_error(f"Fehler in onAVStarted: {str(e)}")

class RadioMonitor(xbmc.Monitor):
    """
    Hauptklasse für das Monitoring und die Verwaltung von Radio-Streams, Metadaten und Player-Events.
    Verantwortlich für das Setzen und Löschen von Properties, das Aktualisieren von Metadaten und das Handling von API-Fallbacks.
    """
    API_SOURCE_NONE = ''
    API_SOURCE_RADIODE = 'radiode'
    API_SOURCE_TUNEIN = 'tunein'
    QF_SERVICE_ADDON_ID = _QF_SERVICE_ADDON_ID
    STREAM_SOURCE_FAMILIES = _STREAM_SOURCE_FAMILIES
    MP_SOURCE_FAMILY = 'musicplayer'
    RADIODE_PLUGIN_IDS = _RADIODE_PLUGIN_IDS
    TUNEIN_PLUGIN_IDS = _TUNEIN_PLUGIN_IDS
    RADIODE_URL_HINTS = ('radio.de', 'radio-assets.com')
    TUNEIN_URL_HINTS = ('tunein.com', 'radiotime.com', 'cdn-profiles.tunein.com')
    MB_WINNER_MIN_SCORE = _MB_WINNER_MIN_SCORE
    MB_WINNER_MIN_COMBINED = _MB_WINNER_MIN_COMBINED
    MP_TRUST_MAX_MISMATCHES = _MP_TRUST_MAX_MISMATCHES
    MP_DECISION_ENABLED = bool(_MP_DECISION_ENABLED)
    TRIGGER_TITLE_CHANGE = _TRIGGER_TITLE_CHANGE
    TRIGGER_API_CHANGE = _TRIGGER_API_CHANGE
    TRIGGER_MP_CHANGE = _TRIGGER_MP_CHANGE
    TRIGGER_MP_INVALID = _TRIGGER_MP_INVALID
    TRIGGER_ICY_STALE = _TRIGGER_ICY_STALE
    TRIGGER_QF_CHANGE = _TRIGGER_QF_CHANGE
    MP_GENERIC_HOLD_MAX_S = 120.0
    STATION_NAME_MATCH_MIN_LEN = int(STATION_NAME_MATCH_MIN_LEN)

    def __init__(self):
        super(RadioMonitor, self).__init__()
        self.player = xbmc.Player()
        self.is_playing = False
        self.current_url = None
        self.metadata_thread = None
        self.stop_thread = False
        self.metadata_generation = 0  # invalidates stale workers on restart
        self.station_id = None    # radio.de Station ID
        self.station_logo = None  # Logo URL von radio.de API
        self._logo_locked_for_session = False  # Logo nur einmal pro Session setzen
        self.station_slug = None  # Sender-Slug aus Stream-URL (für API-Fallback)
        self.plugin_slug = None   # Sender-Slug aus radio.de light Plugin-URL (iconimage)
        self.tunein_station_id = None  # TuneIn Station-ID (z.B. s12345)
        self.api_source = self.API_SOURCE_NONE  # zentrale API-Quellsteuerung (radiode/tunein/none)
        self._last_api_skip_log = None  # dedupliziert wiederholte "API uebersprungen"-Logs
        self.use_api_fallback = False  # Flag für API-Fallback
        # Zentrale Song-Timeout-Status (wird von Metadata-Workern geteilt)
        self._last_song_time = 0.0
        self._song_timeout = SONG_TIMEOUT_FALLBACK_S
        # Nach Song-Timeout: gleicher API-Song wird so lange ignoriert, bis API einen neuen Song liefert.
        self._api_timeout_block_key = ('', '')  # (artist, title)
        self._last_seen_api_key = ('', '')      # zuletzt gelesener API-Kandidat (artist, title)
        self._last_api_now_refresh_ts = 0.0
        self._latest_api_pair = ('', '')
        self._last_decision_source = ''
        self._last_decision_pair = ('', '')
        self._parse_prev_winner_pair = ('', '')
        self._parse_trigger_reason = ''
        self._parse_locked_source = ''
        self._policy_preferred_source = ''
        self._last_policy_context = {}
        self._last_icy_format_hint = 'unknown'
        self._active_station_profile_key = ''
        self._station_profile_session = None
        self._station_profile_policy_enabled = False
        self._active_policy_profile = {}
        self._last_station_profile_observe_ts = 0.0
        self._last_verified_source_write = ('', '', '', 0.0)
        self._qf_enabled = False
        self._last_qf_install_request_ts = 0.0
        self._last_qf_result = ''
        self._qf_request_seq = 0
        self._last_qf_request_id = ''
        self._last_qf_request_station = ''
        self._last_qf_request_ts = 0.0
        self._last_qf_response_id = ''
        self._last_qf_response_match_ts = 0.0
        self._mp_generic_hold_active = False
        self._mp_generic_hold_since_ts = 0.0
        self._mp_generic_hold_timed_out = False
        self.source_policy = SourcePolicy(
            window=SOURCE_POLICY_WINDOW,
            switch_margin=SOURCE_POLICY_SWITCH_MARGIN,
            single_confirm_polls=SOURCE_POLICY_SINGLE_CONFIRM_POLLS
        )
        self._profile_store = self._init_station_profile_store()
        self.musicplayer_trust = MusicPlayerTrust(
            max_mismatches=self.MP_TRUST_MAX_MISMATCHES,
            log_info=log_info,
            log_debug=log_debug,
            log_warning=log_warning
        )
        self.startup_qualifier = StartupQualifier(
            has_non_generic_song_pair=self._has_non_generic_song_pair,
            get_station_profile_hints=self._get_station_profile_hints,
            api_only_stable_polls=STARTUP_API_ONLY_STABLE_POLLS
        )
        self.api_client = APIClient(headers=DEFAULT_HTTP_HEADERS)
        self.raw_sources = RawSourceLabels(WINDOW, log_debug=log_debug)
        self.song_end_detector = SongEndDetector()
        self.song_end_detector_enabled = bool(SONG_END_DETECTOR_ENABLED)
        self.analysis_enabled = bool(ANALYSIS_ENABLED)
        self._analysis_seq = 0
        self.analysis_store = self._init_analysis_store() if self.analysis_enabled else None
        self.mp_decision_enabled = bool(self.MP_DECISION_ENABLED)
        
        # Event-Handler für Player-Events
        self.player_monitor = PlayerMonitor(self)
        self._load_bullet_settings()

        log_info("Service gestartet")

    def _reset_api_context(self):
        """Setzt API-relevanten Zustand zentral zurück."""
        self.station_id = None
        self.plugin_slug = None
        self.station_slug = None
        self.tunein_station_id = None
        self.api_source = self.API_SOURCE_NONE
        self._last_api_skip_log = None
        self.use_api_fallback = False

    def _set_api_source(self, source):
        """Setzt die erlaubte API-Quelle zentral."""
        if source in (self.API_SOURCE_RADIODE, self.API_SOURCE_TUNEIN):
            self.api_source = source
        else:
            self.api_source = self.API_SOURCE_NONE
        self._last_api_skip_log = None

    def _reconcile_api_source(self, context=''):
        """
        Korrigiert die API-Quelle aus starken Laufzeit-Indikatoren.
        Prioritaet:
        - nur plugin_slug vorhanden -> radio.de
        - nur tunein_station_id vorhanden -> tunein
        - beide/keine vorhanden -> keine automatische Umstellung
        """
        target = self.API_SOURCE_NONE
        if self.plugin_slug and not self.tunein_station_id:
            target = self.API_SOURCE_RADIODE
        elif self.tunein_station_id and not self.plugin_slug:
            target = self.API_SOURCE_TUNEIN

        if target in (self.API_SOURCE_RADIODE, self.API_SOURCE_TUNEIN) and target != self.api_source:
            prev = self.api_source or 'none'
            self._set_api_source(target)
            log_info(
                f"API-Source korrigiert "
                f"(context={context}, from={prev}, to={target})"
            )
        return self.api_source

    def _infer_api_source_from_text(self, text):
        """
        Leitet die API-Quelle aus Plugin- oder Stream-URL ab.
        Nutzt zentrale Hints (Plugin-IDs, Query-Parameter, Hostname).
        """
        if not text:
            return self.API_SOURCE_NONE

        try:
            raw = str(text)
            lowered = raw.lower()

            for addon_id in self.RADIODE_PLUGIN_IDS:
                if addon_id in lowered:
                    return self.API_SOURCE_RADIODE
            for addon_id in self.TUNEIN_PLUGIN_IDS:
                if addon_id in lowered:
                    return self.API_SOURCE_TUNEIN

            parsed = urlparse(raw)
            query = parse_qs(parsed.query)
            aggregators = [str(v).lower() for v in query.get('aggregator', [])]
            for value in aggregators:
                if 'tunein' in value:
                    return self.API_SOURCE_TUNEIN
                if 'radio-de' in value or 'radiode' in value:
                    return self.API_SOURCE_RADIODE

            host = (parsed.netloc or '').lower()
            if any(hint in host for hint in self.TUNEIN_URL_HINTS):
                return self.API_SOURCE_TUNEIN
            if any(hint in host for hint in self.RADIODE_URL_HINTS):
                return self.API_SOURCE_RADIODE
        except Exception:
            pass

        return self.API_SOURCE_NONE

    def _ensure_api_source_from_context(self, text, context):
        """
        Setzt api_source nur dann automatisch, wenn noch keine whitelisted
        Quelle erkannt wurde.
        """
        if self._is_api_source_allowed():
            return self.api_source

        inferred = self._infer_api_source_from_text(text)
        if inferred in (self.API_SOURCE_RADIODE, self.API_SOURCE_TUNEIN):
            self._set_api_source(inferred)
            log_info(
                f"API-Source automatisch erkannt "
                f"(context={context}, source={inferred})")
        return self.api_source

    def _is_api_source_allowed(self):
        return self.api_source in (self.API_SOURCE_RADIODE, self.API_SOURCE_TUNEIN)

    def _can_use_radiode_api(self):
        return self.api_source == self.API_SOURCE_RADIODE

    def _can_use_tunein_api(self):
        return self.api_source == self.API_SOURCE_TUNEIN

    def _log_api_source_blocked(self, context):
        """Einheitliches, dedupliziertes Log wenn API-Nutzung wegen Source-Whitelist geblockt wird."""
        source = self.api_source if self.api_source else 'none'
        key = f"{context}:{source}"
        if self._last_api_skip_log == key:
            return
        self._last_api_skip_log = key
        log_debug(
            f"API uebersprungen: Source nicht whitelisted (context={context}, source={source})")

    def _init_station_profile_store(self):
        """
        Initializes the persistent station profile store.
        """
        try:
            profile_path = ''
            if xbmcvfs is not None:
                profile_path = xbmcvfs.translatePath(ADDON.getAddonInfo('profile'))
            if not profile_path:
                profile_path = os.path.join(os.path.dirname(__file__), 'profile')
            profile_dir = os.path.join(profile_path, STATION_PROFILE_DIRNAME)
            return StationProfileStore(profile_dir)
        except Exception as e:
            log_debug(f"Station profile store konnte nicht initialisiert werden: {e}")
            return None

    def _init_analysis_store(self):
        """
        Initializes the persistent analysis event store.
        """
        try:
            profile_path = ''
            if xbmcvfs is not None:
                profile_path = xbmcvfs.translatePath(ADDON.getAddonInfo('profile'))
            if not profile_path:
                profile_path = os.path.join(os.path.dirname(__file__), 'profile')
            return AnalysisEventStore(
                base_dir=profile_path,
                filename=ANALYSIS_EVENTS_FILENAME,
                max_events=ANALYSIS_MAX_EVENTS,
                flush_interval_s=ANALYSIS_FLUSH_INTERVAL_S,
                log_debug=log_debug
            )
        except Exception as e:
            log_debug(f"Analysis store konnte nicht initialisiert werden: {e}")
            return None

    def _build_station_profile_key(self, station_name=''):
        """
        Returns a stable station key for profile learning and lookup.
        """
        if self.plugin_slug:
            return f"radiode:{self.plugin_slug}"
        if self.tunein_station_id:
            return f"tunein:{self.tunein_station_id}"
        if self.station_slug:
            return f"stream:{self.station_slug}"
        name = (station_name or '').strip().lower()
        if not name:
            return ''
        name = re.sub(r'\s+', ' ', name)
        return f"name:{name}"

    def _clear_verified_source_properties(self):
        WINDOW.clearProperty(_P.VERIFIED_SOURCE_URL)
        WINDOW.clearProperty(_P.VERIFIED_SOURCE_BY)
        WINDOW.clearProperty(_P.VERIFIED_SOURCE_CONF)

    def _set_verified_source_properties(self, source_row):
        if not isinstance(source_row, dict):
            self._clear_verified_source_properties()
            return
        source_url = str(source_row.get('source_url', '') or '').strip()
        verified_by = str(source_row.get('verified_by', '') or '').strip()
        confidence = source_row.get('confidence', '')
        if source_url:
            WINDOW.setProperty(_P.VERIFIED_SOURCE_URL, source_url)
        else:
            WINDOW.clearProperty(_P.VERIFIED_SOURCE_URL)
        if verified_by:
            WINDOW.setProperty(_P.VERIFIED_SOURCE_BY, verified_by)
        else:
            WINDOW.clearProperty(_P.VERIFIED_SOURCE_BY)
        try:
            conf_text = f"{float(confidence):.3f}"
        except Exception:
            conf_text = ''
        if conf_text:
            WINDOW.setProperty(_P.VERIFIED_SOURCE_CONF, conf_text)
        else:
            WINDOW.clearProperty(_P.VERIFIED_SOURCE_CONF)

    def _resolve_verified_station_for_url(self, stream_url):
        if self._profile_store is None:
            return {}
        source_url = str(stream_url or '').strip()
        if not source_url:
            return {}
        try:
            row = self._profile_store.get_verified_source_by_url(source_url)
            if isinstance(row, dict):
                return row
        except Exception as e:
            log_debug(f"Verified-source Lookup fehlgeschlagen: {e}")
        return {}

    def _apply_verified_source_hint(self, stream_url):
        source_url = str(stream_url or '').strip()
        if not source_url:
            self._clear_verified_source_properties()
            return ''
        row = self._resolve_verified_station_for_url(source_url)
        self._set_verified_source_properties(row if row else None)
        station_name = str((row or {}).get('station_name', '') or '').strip()
        if station_name and not (WINDOW.getProperty(_P.STATION) or '').strip():
            self.set_property_safe(_P.STATION, station_name)
            log_info(
                f"Station aus verified source gesetzt: '{station_name}' "
                f"(by='{(row or {}).get('verified_by', '') or 'unknown'}')"
            )
        return station_name

    def _record_verified_station_source(
        self,
        station_name='',
        stream_url='',
        source_kind='asm_runtime',
        verified_by='',
        confidence=0.75,
        meta=None,
    ):
        if not (self._persist_data and self._profile_store is not None):
            return False
        name = str(station_name or WINDOW.getProperty(_P.STATION) or '').strip()
        source_url = str(stream_url or self.current_url or '').strip()
        if not name or not source_url:
            return False
        station_key = self._build_station_profile_key(name)
        if not station_key:
            return False
        source_kind_text = str(source_kind or 'asm_runtime')
        now_ts = time.time()
        dedup_key = (station_key, source_url, source_kind_text)
        last_station_key, last_source_url, last_kind, last_ts = self._last_verified_source_write
        if (
            dedup_key == (last_station_key, last_source_url, last_kind)
            and (now_ts - float(last_ts or 0.0)) < 30.0
        ):
            return False
        verifier = str(verified_by or ADDON_ID).strip().lower()
        try:
            conf_value = float(confidence)
        except Exception:
            conf_value = 0.75
        conf_value = max(0.0, min(1.0, conf_value))
        details = {
            'source_kind': source_kind_text,
            'api_source': str(self.api_source or ''),
            'plugin_slug': str(self.plugin_slug or ''),
            'tunein_station_id': str(self.tunein_station_id or ''),
        }
        if isinstance(meta, dict):
            details.update(meta)
        elif meta is not None:
            details['meta'] = str(meta)
        try:
            ok = self._profile_store.record_verified_source(
                station_key=station_key,
                source_url=source_url,
                station_name=name,
                source_kind=source_kind_text,
                verified_by=verifier,
                confidence=conf_value,
                meta=details,
            )
            if ok:
                self._last_verified_source_write = (
                    station_key, source_url, source_kind_text, now_ts
                )
            return bool(ok)
        except Exception as e:
            log_debug(f"Verified-source Persist fehlgeschlagen: {e}")
            return False

    def _persist_confirmed_song_if_allowed(self, station_name, artist, title, mbid):
        """
        Persistiert Song-Counts nur fuer MB-verifizierte Songpaare.
        """
        if not (self._persist_data and self._profile_store and artist and title):
            return
        if not mbid:
            log_debug(
                f"Song DB persist uebersprungen (kein MB-Verify): "
                f"'{artist} - {title}'"
            )
            return
        station_key = self._build_station_profile_key(station_name)
        if station_key:
            self._profile_store.record_confirmed_song(station_key, artist, title)

    def _is_song_pair(self, pair):
        return _is_song_pair(pair)

    def _is_generic_metadata_text(self, text, station_name=''):
        kw = self._get_station_generic_keywords(station_name)
        return _is_generic_metadata_text(text, station_name, kw)

    def _is_obvious_non_song_text(self, text, station_name=''):
        kw = self._get_station_generic_keywords(station_name)
        return _pre_mb_is_obvious_non_song_text(text, extra_keywords=kw)

    @staticmethod
    def _normalize_station_compare_text(text):
        return _normalize_station_compare_text(text)

    def _station_name_compare_hints(self, station_name=''):
        return _build_station_hints((
            station_name,
            WINDOW.getProperty(_P.STATION),
            self.station_slug,
            self.plugin_slug,
            self.station_id,
        ))

    def _is_station_name_match_pair(self, pair, station_name=''):
        return _is_station_name_match_pair(
            pair,
            station_hints=self._station_name_compare_hints(station_name),
            min_len=self.STATION_NAME_MATCH_MIN_LEN,
        )

    def _is_generic_song_pair(self, pair, station_name=''):
        kw = self._get_station_generic_keywords(station_name)
        return _is_generic_song_pair(pair, station_name, kw)

    def _has_non_generic_song_pair(self, pair, station_name=''):
        kw = self._get_station_generic_keywords(station_name)
        return _has_non_generic_song_pair(pair, station_name, kw)

    def _is_generic_stream_title(self, stream_title, station_name=''):
        kw = self._get_station_generic_keywords(station_name)
        return _is_generic_metadata_text(stream_title, station_name, kw)

    def _filter_non_generic_song_pairs(self, pairs, station_name=''):
        kw = self._get_station_generic_keywords(station_name)
        return _filter_non_generic_song_pairs(pairs, station_name, kw)

    def _sanitize_pre_mb_pair(self, pair, station_name='', source='', reject_obvious_text=False):
        effective_station = str(station_name or WINDOW.getProperty(_P.STATION) or '').strip()
        invalid_values = INVALID_METADATA_VALUES + ['', effective_station]
        source_name = str(source or '').strip().lower()
        reject_station_match = bool(
            source_name.startswith('api')
            or source_name.startswith('icy')
            or source_name.startswith('asm-qf')
            or source_name in ('stream', '')
        )
        kw = self._get_station_generic_keywords(effective_station)
        return _sanitize_pair_for_pre_mb(
            pair,
            station_name=effective_station,
            invalid_values=invalid_values,
            extra_keywords=kw,
            station_hints=self._station_name_compare_hints(effective_station),
            station_match_min_len=self.STATION_NAME_MATCH_MIN_LEN,
            reject_generic=True,
            reject_station_match=reject_station_match,
            reject_obvious_text=bool(reject_obvious_text),
        )

    def _is_pre_mb_song_pair(self, pair, station_name='', source=''):
        effective_station = str(station_name or WINDOW.getProperty(_P.STATION) or '').strip()
        invalid_values = INVALID_METADATA_VALUES + ['', effective_station]
        source_name = str(source or '').strip().lower()
        reject_station_match = bool(
            source_name.startswith('api')
            or source_name.startswith('icy')
            or source_name.startswith('asm-qf')
            or source_name in ('stream', '')
        )
        kw = self._get_station_generic_keywords(effective_station)
        return _is_pre_mb_plausible_pair(
            pair,
            station_name=effective_station,
            invalid_values=invalid_values,
            extra_keywords=kw,
            station_hints=self._station_name_compare_hints(effective_station),
            station_match_min_len=self.STATION_NAME_MATCH_MIN_LEN,
            reject_generic=True,
            reject_station_match=reject_station_match,
            reject_obvious_text=True,
        )

    def _sanitize_musicplayer_pair(self, pair, station_name=''):
        return self._sanitize_pre_mb_pair(
            pair,
            station_name=station_name,
            source='musicplayer',
            reject_obvious_text=False,
        )

    def _sanitize_stream_source_pair(self, pair, station_name=''):
        return self._sanitize_pre_mb_pair(
            pair,
            station_name=station_name,
            source='stream',
            reject_obvious_text=False,
        )

    def _append_non_generic_candidate(self, candidates, source, artist, title, station_name=''):
        pair = self._sanitize_pre_mb_pair(
            (artist, title),
            station_name=station_name,
            source=str(source or ''),
            reject_obvious_text=False,
        )
        if not pair[0] or not pair[1]:
            log_debug(
                f"Kandidat verworfen (pre_mb_policy): source='{source}', "
                f"pair='{str(artist or '').strip()} - {str(title or '').strip()}'"
            )
            return False
        candidates.append({'source': str(source or ''), 'artist': pair[0], 'title': pair[1]})
        return True

    def _update_mp_generic_hold_state(self, last_winner_source, current_mp_pair, station_name=''):
        """
        Haelt die zuletzt valide MP-Quelle waehrend einer generischen MP-Phase.
        Verhindert Quellspruenge auf ICY/API bei Nachrichten/Jingles.
        """
        source = str(last_winner_source or '')
        mp_has_song = self._has_non_generic_song_pair(current_mp_pair, station_name)

        if not source.startswith('musicplayer'):
            self._mp_generic_hold_active = False
            self._mp_generic_hold_since_ts = 0.0
            self._mp_generic_hold_timed_out = False
            return False

        if mp_has_song:
            if self._mp_generic_hold_active:
                elapsed = max(0, int(round(time.time() - self._mp_generic_hold_since_ts)))
                log_info(
                    f"MP-Generic-Hold beendet: MP liefert wieder Songdaten "
                    f"(dauer={elapsed}s)"
                )
            self._mp_generic_hold_active = False
            self._mp_generic_hold_since_ts = 0.0
            self._mp_generic_hold_timed_out = False
            return False

        if self._mp_generic_hold_timed_out:
            return False

        now_ts = time.time()
        if not self._mp_generic_hold_active:
            self._mp_generic_hold_active = True
            self._mp_generic_hold_since_ts = now_ts
            log_info(f"MP-Generic-Hold aktiv: MP aktuell generisch/leer")
            return True

        if (now_ts - self._mp_generic_hold_since_ts) >= float(self.MP_GENERIC_HOLD_MAX_S):
            self._mp_generic_hold_active = False
            self._mp_generic_hold_timed_out = True
            log_info(
                f"MP-Generic-Hold Timeout ({int(self.MP_GENERIC_HOLD_MAX_S)}s): "
                f"ICY/API duerfen wieder triggern"
            )
            return False

        return True

    def _pairs_match_or_swapped(self, p1, p2):
        if not (self._is_song_pair(p1) and self._is_song_pair(p2)):
            return False
        if p1 == p2:
            return True
        return (p1[0] == p2[1] and p1[1] == p2[0])

    def _maybe_reclaim_musicplayer_source(self, decision_source, decision_pair, current_mp_pair, station_name=''):
        """
        Wenn ICY/API gewinnt, MP aber zeitgleich exakt dasselbe Song-Paar liefert,
        wird die Gewinnerquelle auf MP zurueckgenommen (Source-Reclaim).
        """
        source = str(decision_source or '')
        if source.startswith('musicplayer'):
            return decision_source, decision_pair
        if not self._has_non_generic_song_pair(current_mp_pair, station_name):
            return decision_source, decision_pair
        if not self._is_song_pair(decision_pair):
            return decision_source, decision_pair
        if not self._pairs_match_or_swapped(current_mp_pair, decision_pair):
            return decision_source, decision_pair

        reclaimed_source = 'musicplayer_reclaim'
        self._set_last_song_decision(reclaimed_source, current_mp_pair[0], current_mp_pair[1])
        log_info(
            f"MP-Reclaim aktiv: Quelle auf musicplayer zurueckgesetzt "
            f"('{current_mp_pair[0]} - {current_mp_pair[1]}')"
        )
        return reclaimed_source, current_mp_pair

    def _current_profile_confidence(self):
        try:
            return float((self._active_policy_profile or {}).get('confidence', 0.0))
        except Exception:
            return 0.0

    def _has_station_analysis(self):
        if self._current_profile_confidence() < 0.20:
            return False
        preferred = str((self._active_policy_profile or {}).get('preferred_family', '') or '')
        return preferred in ('musicplayer', 'api', 'icy')

    def _is_mp_profile_reliable(self):
        profile = self._active_policy_profile or {}
        try:
            confidence = float(profile.get('confidence', 0.0))
        except Exception:
            confidence = 0.0
        if confidence < 0.20:
            return False
        if bool(profile.get('mp_noise', False)) or bool(profile.get('mp_absent', False)):
            return False
        return bool(profile.get('mp_reliable', False))

    def _is_mp_decision_active(self):
        return bool(self.mp_decision_enabled or self._is_mp_profile_reliable())

    def _effective_icy_format_hint(self):
        live_hint = str(self._last_icy_format_hint or '').strip().lower()
        if live_hint in ('artist_title', 'title_artist'):
            return live_hint

        profile = self._active_policy_profile or {}
        try:
            confidence = float(profile.get('confidence', 0.0))
        except Exception:
            confidence = 0.0
        if confidence < 0.20:
            return 'unknown'

        profile_hint = str(profile.get('icy_format', '') or '').strip().lower()
        if profile_hint in ('artist_title', 'title_artist'):
            return profile_hint
        return 'unknown'

    def _prefer_icy_swapped_from_history(self):
        profile = self._active_policy_profile or {}
        try:
            confidence = float(profile.get('confidence', 0.0))
        except Exception:
            confidence = 0.0
        if confidence < 0.20:
            return False
        return bool(profile.get('icy_prefer_swapped', False))

    def _should_prioritize_stream_candidates(self):
        if not self._has_station_analysis():
            return False
        preferred = str((self._active_policy_profile or {}).get('preferred_family', '') or '')
        return preferred in ('api', 'icy')

    def _get_station_policy_profile(self, station_name=''):
        if isinstance(self._active_policy_profile, dict) and self._active_policy_profile:
            return dict(self._active_policy_profile)
        if self._profile_store is None:
            return {}
        station_key = self._build_station_profile_key(station_name)
        if not station_key:
            return {}
        try:
            return dict(self._profile_store.get_policy_profile(station_key) or {})
        except Exception:
            return {}

    def _get_station_profile_hints(self, station_name=''):
        profile = self._get_station_policy_profile(station_name)
        confidence = 0.0
        try:
            confidence = float(profile.get('confidence', 0.0))
        except Exception:
            confidence = 0.0
        return {
            'confidence': confidence,
            'icy_structural_generic': bool(profile.get('icy_structural_generic', False)),
            'mp_noise': bool(profile.get('mp_noise', False)),
            'mp_absent': bool(profile.get('mp_absent', False)),
        }

    def _get_station_generic_keywords(self, station_name=''):
        """Liest generic_keywords des Senders aus dem StationProfileStore."""
        if self._profile_store is None:
            return ()
        station_key = self._build_station_profile_key(station_name)
        if not station_key:
            return ()
        try:
            return self._profile_store.get_generic_keywords(station_key)
        except Exception:
            return ()

    def _default_song_end_policy(self, station_name=''):
        return {
            'enabled': bool(SONG_END_DETECTOR_ENABLED),
            'min_song_age_s': float(SONG_END_MIN_SONG_AGE_S),
            'hold_s': float(SONG_END_HOLD_S),
            'min_keyword_hits': int(SONG_END_MIN_KEYWORD_HITS),
            'min_non_song_sources': int(SONG_END_MIN_NON_SONG_SOURCES),
            'require_additional_signal': bool(SONG_END_REQUIRE_ADDITIONAL_SIGNAL),
            'stale_api_min_s': float(SONG_END_STALE_API_MIN_S),
            'near_timeout_s': float(SONG_END_NEAR_TIMEOUT_S),
            'generic_keywords': list(self._get_station_generic_keywords(station_name)),
        }

    def _get_station_song_end_policy(self, station_name=''):
        policy = self._default_song_end_policy(station_name)
        if self._profile_store is None:
            return policy
        station_key = self._build_station_profile_key(station_name)
        if not station_key:
            return policy
        try:
            stored = self._profile_store.get_song_end_policy(station_key)
        except Exception:
            stored = None
        if isinstance(stored, dict):
            for key in (
                'enabled', 'min_song_age_s', 'hold_s', 'min_keyword_hits',
                'min_non_song_sources', 'require_additional_signal',
                'stale_api_min_s', 'near_timeout_s'
            ):
                if key in stored:
                    policy[key] = stored.get(key)
        policy['generic_keywords'] = list(self._get_station_generic_keywords(station_name))
        return policy

    def _record_station_keyword_stats(self, station_name, candidates):
        if not self._persist_data or self._profile_store is None:
            return
        station_key = self._build_station_profile_key(station_name)
        if not station_key:
            return
        candidate_list = [str(item or '').strip().lower() for item in list(candidates or []) if str(item or '').strip()]
        if not candidate_list:
            return
        try:
            self._profile_store.record_keyword_candidates(station_key, candidate_list)
            self._profile_store.flush_if_due(min_interval_s=STATION_PROFILE_SAVE_INTERVAL_S)
        except Exception as e:
            log_debug(f"Keyword-Statistik konnte nicht aktualisiert werden: {e}")

    def _collect_keyword_observations(self, station_name, texts):
        """Extrahiert Generic-Kandidaten aus Quelltexten (nur außerhalb bestätigter Songs)."""
        if not station_name:
            return
        configured = self._get_station_generic_keywords(station_name)
        tokens = SongEndDetector.extract_candidate_keywords(texts, station_name, configured)
        if tokens:
            self._record_station_keyword_stats(station_name, tokens)

    @staticmethod
    def _label_from_pair(pair):
        if not (pair and pair[0] and pair[1]):
            return ''
        return f"{pair[0]} - {pair[1]}"

    def _get_aux_source_pairs_for_song_end(self, station_name=''):
        invalid_values = INVALID_METADATA_VALUES + ["", station_name]
        raw_listitem = WINDOW.getProperty(_P.RAW_LISTITEM) or ''
        raw_playing_item = WINDOW.getProperty(_P.RAW_PLAYING_ITEM) or ''
        raw_jsonrpc = WINDOW.getProperty(_P.RAW_JSONRPC_PLAYER) or ''

        listitem_pair = self._normalize_song_candidate(*_extract_listitem_pair(raw_listitem), invalid_values)
        playing_item_pair = self._normalize_song_candidate(*_extract_playing_item_pair(raw_playing_item), invalid_values)
        jsonrpc_pair = self._normalize_song_candidate(*_extract_jsonrpc_pair(raw_jsonrpc), invalid_values)

        return {
            'listitem': (listitem_pair[0] or '', listitem_pair[1] or ''),
            'playing_item': (playing_item_pair[0] or '', playing_item_pair[1] or ''),
            'jsonrpc': (jsonrpc_pair[0] or '', jsonrpc_pair[1] or ''),
        }

    def _classify_icy_format(self, stream_title, station_name=''):
        text = (stream_title or '').strip()
        if not text:
            return 'unknown'
        try:
            artist, title, is_von_format, _ = _parse_metadata_complex(text, station_name)
        except Exception:
            return 'unknown'
        artist = (artist or '').strip()
        title = (title or '').strip()
        if not artist or not title:
            return 'unknown'
        return 'title_artist' if bool(is_von_format) else 'artist_title'

    def _refresh_station_profile_context(self, station_name='', enable_policy=False):
        if self._profile_store is None:
            return

        key = self._build_station_profile_key(station_name)
        if not key:
            return

        key_changed = (key != self._active_station_profile_key)
        if key_changed:
            self._close_station_profile_session(persist=bool(self._persist_data))
            self._active_station_profile_key = key
            self._station_profile_session = self._profile_store.start_session(key, station_name)
            self._station_profile_policy_enabled = False
            self._active_policy_profile = {}
            self.source_policy.clear_station_profile()
            log_debug(f"Station-Profil Session gestartet: key='{key}'")

        if enable_policy and not key_changed and not self._station_profile_policy_enabled:
            policy_profile = self._profile_store.get_policy_profile(key)
            self.source_policy.apply_station_profile(policy_profile)
            self.source_policy.set_generic_keywords(self._profile_store.get_generic_keywords(key))
            self.source_policy.set_known_songs(self._profile_store.get_known_songs(key))
            self._station_profile_policy_enabled = True
            self._active_policy_profile = dict(policy_profile or {})
            log_info(
                f"Station-Profil aktiv: key='{key}', "
                f"confidence={policy_profile.get('confidence', 0.0):.2f}, "
                f"preferred='{policy_profile.get('preferred_family', '') or 'none'}'"
            )

    def _try_enable_station_profile_policy(
        self,
        station_name,
        startup_stable_confirmed,
        current_icy_pair,
        current_api_pair
    ):
        if self._profile_store is None:
            return
        if self._station_profile_policy_enabled:
            return
        if not startup_stable_confirmed:
            return
        icy_song_ready = (
            self._is_song_pair(current_icy_pair)
            and not self._is_generic_song_pair(current_icy_pair, station_name)
        )
        api_only_ready = self.startup_qualifier.profile_api_only_ready(station_name, current_api_pair)
        if not (icy_song_ready or api_only_ready):
            return

        self._refresh_station_profile_context(station_name, enable_policy=True)

    def _update_station_profile(self, station_name=''):
        if self._profile_store is None:
            return
        self._refresh_station_profile_context(station_name, enable_policy=self._station_profile_policy_enabled)
        if self._station_profile_session is None:
            return

        now_ts = time.time()
        if (now_ts - self._last_station_profile_observe_ts) < STATION_PROFILE_OBSERVE_INTERVAL_S:
            return
        self._last_station_profile_observe_ts = now_ts

        observation = self.source_policy.latest_observation()
        if not observation:
            return

        context = dict(self._last_policy_context or {})
        context['station_name'] = station_name
        context['icy_format'] = self._last_icy_format_hint
        context['winner_source_detail'] = str(self._last_decision_source or '')
        context['icy_is_song'] = bool(
            self._is_song_pair(context.get('current_icy_pair'))
            and not self._is_generic_song_pair(context.get('current_icy_pair'), station_name)
        )
        self._station_profile_session.observe(observation, context)
        self._profile_store.flush_if_due(min_interval_s=STATION_PROFILE_SAVE_INTERVAL_S)

    def _close_station_profile_session(self, persist=True):
        if self._profile_store is None or self._station_profile_session is None:
            return
        try:
            if persist:
                profile = self._profile_store.finish_session(self._station_profile_session)
                if profile is not None:
                    log_debug(
                        f"Station-Profil Session gespeichert: key='{self._station_profile_session.station_key}', "
                        f"sessions={profile.get('sessions', 0)}, "
                        f"stable={profile.get('sessions_above_threshold', 0)}, "
                        f"confidence={profile.get('confidence', 0.0):.2f}"
                    )
        finally:
            self._station_profile_session = None
            self._station_profile_policy_enabled = False
            self._active_policy_profile = {}
            self._last_station_profile_observe_ts = 0.0
            self.source_policy.clear_station_profile()

    def _flush_station_profiles(self):
        if self._profile_store is None:
            return
        self._close_station_profile_session(persist=bool(self._persist_data))
        self._active_station_profile_key = ''
        if self._persist_data:
            self._profile_store.flush()

    def _clear_timer_debug_properties(self):
        """Loescht Debug-Properties fuer MB-Dauer und Song-Timer."""
        WINDOW.clearProperty(_P.MB_DUR_MS)
        WINDOW.clearProperty(_P.MB_DUR_S)
        WINDOW.clearProperty(_P.TMO_TOTAL)
        WINDOW.clearProperty(_P.TMO_LEFT)

    def _reset_song_timeout_state(self, clear_debug=False):
        """Setzt den Song-Timer-Zustand zentral zurueck."""
        self._last_song_time = 0.0
        self._song_timeout = SONG_TIMEOUT_FALLBACK_S
        if clear_debug:
            self._clear_timer_debug_properties()

    def _reset_musicplayer_trust_state(self, reason=''):
        """Setzt den MusicPlayer-Trust-Zustand zentral zurueck."""
        self.musicplayer_trust.reset(self.metadata_generation, reason=reason)

    def _update_timeout_remaining_property(self):
        """Aktualisiert den verbleibenden Song-Timer fuer Skin-Debugging."""
        if self._last_song_time and self._song_timeout > 0:
            elapsed = time.time() - self._last_song_time
            remaining = max(0, int(self._song_timeout - elapsed))
            WINDOW.setProperty(_P.TMO_LEFT, str(remaining))
        else:
            WINDOW.clearProperty(_P.TMO_LEFT)

    def _compute_song_timeout(self, duration_ms):
        """
        Berechnet zentral den Song-Timeout:
        - bei MB-Laenge: Songlaenge minus SONG_TIMEOUT_EARLY_CLEAR_S
        - ohne MB-Laenge: SONG_TIMEOUT_FALLBACK_S
        """
        duration_s = 0.0
        try:
            if duration_ms:
                duration_s = float(duration_ms) / 1000.0
        except Exception:
            duration_s = 0.0

        if duration_s > 0:
            return max(0.0, duration_s - SONG_TIMEOUT_EARLY_CLEAR_S)
        return SONG_TIMEOUT_FALLBACK_S
        
    def _start_song_timeout(self, duration_ms, song_key=('', ''), station_name=''):
        """
        Startet den Song-Timer zentral und setzt Debug-Properties:
        - RadioMonitor.MBDurationMs / RadioMonitor.MBDurationS
        - RadioMonitor.TimeoutTotal / RadioMonitor.TimeoutRemaining
        """
        self._last_song_time = time.time()
        self._song_timeout = self._compute_song_timeout(duration_ms)

        mb_duration_ms = 0
        try:
            if duration_ms:
                mb_duration_ms = int(float(duration_ms))
        except Exception:
            mb_duration_ms = 0

        if mb_duration_ms > 0:
            WINDOW.setProperty(_P.MB_DUR_MS, str(mb_duration_ms))
            WINDOW.setProperty(_P.MB_DUR_S, str(int(round(mb_duration_ms / 1000.0))))
        else:
            WINDOW.clearProperty(_P.MB_DUR_MS)
            WINDOW.clearProperty(_P.MB_DUR_S)

        WINDOW.setProperty(_P.TMO_TOTAL, str(int(round(self._song_timeout))))
        self._update_timeout_remaining_property()

        if mb_duration_ms > 0:
            log_debug(
                f"Song-Timeout: {self._song_timeout:.0f}s "
                f"(MB-Laenge: {mb_duration_ms}ms, -{SONG_TIMEOUT_EARLY_CLEAR_S}s, fallback={SONG_TIMEOUT_FALLBACK_S}s)"
            )
        else:
            log_debug(
                f"Song-Timeout: {self._song_timeout:.0f}s "
                f"(MB-Laenge: unbekannt, fallback={SONG_TIMEOUT_FALLBACK_S}s)"
            )
        if self.song_end_detector_enabled:
            self.song_end_detector.on_song_started(song_key=song_key, station_name=station_name)

    def _clear_song_properties(self, reason_text, last_song_key=('', ''), enable_api_block=False):
        if reason_text:
            log_debug(str(reason_text))

        if enable_api_block and last_song_key[0] and last_song_key[1]:
            self._api_timeout_block_key = (last_song_key[0], last_song_key[1])
            log_debug(
                f"API-Block bis Songwechsel aktiviert: "
                f"'{last_song_key[0]} - {last_song_key[1]}'"
            )

        WINDOW.clearProperty(_P.ARTIST)
        WINDOW.clearProperty(_P.ARTIST_DISPLAY)
        WINDOW.clearProperty(_P.TITLE)
        WINDOW.clearProperty(_P.ALBUM)
        WINDOW.clearProperty(_P.ALBUM_DATE)
        WINDOW.clearProperty(_P.MBID)
        WINDOW.clearProperty(_P.FIRST_REL)
        WINDOW.clearProperty(_P.BAND_FORM)
        WINDOW.clearProperty(_P.BAND_MEM)
        WINDOW.clearProperty(_P.GENRE)
        self._reset_song_timeout_state(clear_debug=True)
        self.song_end_detector.reset()

    def _handle_song_timeout_expiry(self, last_song_key=('', ''), enable_api_block=False):
        """
        Aktualisiert Timeout-Remaining und loescht Song-Properties bei Ablauf.

        Args:
            last_song_key: Letzter gueltiger Song (artist, title), optional fuer API-Block.
            enable_api_block: Aktiviert API-Block bis Titelwechsel nach Timeout.

        Returns:
            bool: True wenn Timeout abgelaufen und geloescht wurde, sonst False.
        """
        self._update_timeout_remaining_property()
        if not (self._last_song_time and time.time() - self._last_song_time > self._song_timeout):
            return False

        self._clear_song_properties(
            reason_text=f"Song-Timeout abgelaufen ({self._song_timeout:.0f}s) - loesche Song-Properties",
            last_song_key=last_song_key,
            enable_api_block=enable_api_block
        )
        return True

    def clear_properties(self):
        """Löscht alle Radio-Properties"""
        if self._profile_store is not None and self._station_profile_session is not None:
            self._flush_station_profiles()

        # Reset Logo und API-Kontext
        self.station_logo = None
        self._logo_locked_for_session = False
        self._reset_api_context()
        self._reset_song_timeout_state(clear_debug=True)
        self._reset_musicplayer_trust_state('clear_properties')
        self._api_timeout_block_key = ('', '')
        self._last_seen_api_key = ('', '')
        self._last_api_now_refresh_ts = 0.0
        self._latest_api_pair = ('', '')
        self._last_decision_source = ''
        self._last_decision_pair = ('', '')
        self._parse_prev_winner_pair = ('', '')
        self._parse_trigger_reason = ''
        self._parse_locked_source = ''
        self._policy_preferred_source = ''
        self._last_policy_context = {}
        self._last_icy_format_hint = 'unknown'
        self._station_profile_policy_enabled = False
        self._active_policy_profile = {}
        self._last_verified_source_write = ('', '', '', 0.0)
        self._last_qf_result = ''
        self._last_qf_request_id = ''
        self._last_qf_request_station = ''
        self._last_qf_request_ts = 0.0
        self._last_qf_response_id = ''
        self._last_qf_response_match_ts = 0.0
        self._mp_generic_hold_active = False
        self._mp_generic_hold_since_ts = 0.0
        self._mp_generic_hold_timed_out = False
        self.song_end_detector.reset()
        self.startup_qualifier.reset_session()
        self.source_policy = SourcePolicy(
            window=SOURCE_POLICY_WINDOW,
            switch_margin=SOURCE_POLICY_SWITCH_MARGIN,
            single_confirm_polls=SOURCE_POLICY_SINGLE_CONFIRM_POLLS
        )

        # Lösche auch radio.de Addon Properties
        WINDOW.clearProperty(_P.RADIODE_LOGO)
        WINDOW.clearProperty(_P.RADIODE_NAME)
        
        # Window-Properties (für Fallback)
        WINDOW.clearProperty(_P.STATION)
        WINDOW.clearProperty(_P.TITLE)
        WINDOW.clearProperty(_P.ARTIST)
        WINDOW.clearProperty(_P.ARTIST_DISPLAY)
        WINDOW.clearProperty(_P.ALBUM)
        WINDOW.clearProperty(_P.ALBUM_DATE)
        WINDOW.clearProperty(_P.GENRE)
        WINDOW.clearProperty(_P.MBID)
        WINDOW.clearProperty(_P.FIRST_REL)
        WINDOW.clearProperty(_P.STREAM_TTL)
        WINDOW.clearProperty(_P.API_NOW)
        WINDOW.clearProperty(_P.ICY_NOW)
        WINDOW.clearProperty(_P.SOURCE)
        WINDOW.clearProperty(_P.SOURCE_DETAIL)
        WINDOW.clearProperty(_P.VERIFIED_SOURCE_URL)
        WINDOW.clearProperty(_P.VERIFIED_SOURCE_BY)
        WINDOW.clearProperty(_P.VERIFIED_SOURCE_CONF)
        WINDOW.clearProperty(_P.QF_REQUEST_ID)
        WINDOW.clearProperty(_P.QF_REQUEST_STATION)
        WINDOW.clearProperty(_P.QF_REQUEST_STATION_ID)
        WINDOW.clearProperty(_P.QF_REQUEST_MODE)
        WINDOW.clearProperty(_P.QF_REQUEST_TS)
        self._clear_qf_response_properties()
        WINDOW.clearProperty(_P.QF_RESULT)
        WINDOW.clearProperty(_P.PLAYING)
        WINDOW.clearProperty(_P.LOGO)
        WINDOW.clearProperty(_P.BAND_FORM)
        WINDOW.clearProperty(_P.BAND_MEM)
        WINDOW.clearProperty(_P.AN_TRACE_ID)
        WINDOW.clearProperty(_P.AN_TRIGGER)
        WINDOW.clearProperty(_P.AN_WINNER_SOURCE)
        WINDOW.clearProperty(_P.AN_WINNER_PAIR)
        WINDOW.clearProperty(_P.AN_LAST_EVENT)
        
        self.raw_sources.clear_all()
        log_debug(f"Properties gelöscht")
        
    def _handle_stream_transition(self, reason=''):
        """
        Streamwechsel: alte Labels sofort leeren, bevor neue Daten gesetzt werden.
        """
        self.stop_metadata_monitoring()
        self._flush_station_profiles()
        self.is_playing = False
        self.current_url = None
        self.clear_properties()
        if reason:
            log_debug(f"Streamwechsel: Labels geleert ({reason})")

    def _handle_playback_stop(self, reason=''):
        """
        Playback-Stop/Ende: Labels sofort leeren (ohne 2s Poll-Wartezeit).
        """
        self.stop_metadata_monitoring()
        self._flush_station_profiles()
        self.is_playing = False
        self.current_url = None
        self.clear_properties()
        if reason:
            log_debug(f"Wiedergabe beendet: Labels sofort geleert ({reason})")

    _BULLET_KEYS = {
        'RadioMonitor.Station', 'RadioMonitor.Title', 'RadioMonitor.ArtistDisplay',
        'RadioMonitor.Album', 'RadioMonitor.Genre',
    }

    def _has_addon(self, addon_id):
        addon_id = str(addon_id or '').strip()
        if not addon_id:
            return False
        try:
            if xbmc.getCondVisibility(f"System.HasAddon({addon_id})"):
                return True
        except Exception:
            pass
        try:
            import xbmcaddon as _xbmcaddon
            _xbmcaddon.Addon(id=addon_id)
            return True
        except Exception:
            return False

    def _ensure_qf_addon_installed(self):
        if not self._qf_enabled:
            return
        if self._has_addon(self.QF_SERVICE_ADDON_ID):
            return
        now_ts = time.time()
        if (now_ts - self._last_qf_install_request_ts) < 30.0:
            return
        self._last_qf_install_request_ts = now_ts
        try:
            xbmc.executebuiltin(f"InstallAddon({self.QF_SERVICE_ADDON_ID})")
            log_info(
                f"ASM-QF aktiviert, Addon fehlt: InstallAddon fuer "
                f"'{self.QF_SERVICE_ADDON_ID}' gestartet"
            )
        except Exception as e:
            log_warning(
                f"ASM-QF aktiviert, Auto-Installation fehlgeschlagen "
                f"('{self.QF_SERVICE_ADDON_ID}'): {e}"
            )

    def _load_bullet_settings(self):
        """Liest Addon-Settings und aktualisiert Bullet-Prefix und Persistenz-Flag."""
        import xbmcaddon as _xbmcaddon
        addon   = _xbmcaddon.Addon(ADDON_ID)
        enabled = addon.getSetting('bullet_enabled').lower() == 'true'
        color   = addon.getSetting('bullet_color') or 'green'
        self._bullet_prefix = f'[COLOR {color}]•[/COLOR] ' if enabled else ''
        self._persist_data  = addon.getSetting('persist_data').lower() != 'false'
        self._qf_enabled = (addon.getSetting('qf_enabled') or 'false').lower() == 'true'
        if not self._persist_data:
            log_info("Datenpersistenz deaktiviert – DB und JSON werden nicht geschrieben")

        self._ensure_qf_addon_installed()

    def onSettingsChanged(self):
        self._load_bullet_settings()
        log_debug("Einstellungen neu geladen (Bullet, Persistenz und ASM-QF Integration aktualisiert)")

    def set_property_safe(self, key, value):
        """Setzt eine Window-Property nur wenn der Wert nicht leer ist."""
        if value:
            text = f"{self._bullet_prefix}{value}" if key in self._BULLET_KEYS else str(value)
            WINDOW.setProperty(key, text)
    
    def is_real_logo(self, url):
        """Prüft ob es ein echtes Logo ist (keine Kodi-Fallbacks)"""
        if not url:
            return False
        invalid = ['DefaultAudio', 'DefaultAlbum', 'no_image', 'no-image', 'default.png', 'Default']
        return not any(x in str(url) for x in invalid)
    
    def set_logo_safe(self):
        """Setzt Logo-Property pro Session genau einmal bei echtem Logo."""
        if self._logo_locked_for_session:
            return
        if self.station_logo and self.is_real_logo(self.station_logo):
            self._ensure_radiode_identity_from_value(self.station_logo, context='set_logo_safe')
            self.set_property_safe(_P.LOGO, self.station_logo)
            self._logo_locked_for_session = True

    def _extract_radiode_slug_from_value(self, value):
        text = str(value or '').strip()
        if not text:
            return ''
        # Beispiel: https://...radio-assets.com/300/starfmberlin.jpeg?version=...
        match = re.search(r'radio-assets\.com/\d+/([^./?]+)', text, flags=re.IGNORECASE)
        if match:
            return str(match.group(1) or '').strip()
        return ''

    def _ensure_radiode_identity_from_value(self, value, context=''):
        slug = self._extract_radiode_slug_from_value(value)
        if not slug:
            return
        if self.plugin_slug != slug:
            self.plugin_slug = slug
            log_debug(f"radio.de Slug aus Kontext gesetzt: '{slug}' (context={context})")
        if not self.station_id:
            self.station_id = slug
            log_debug(f"radio.de Station-ID aus Kontext gesetzt: '{slug}' (context={context})")
        if self.api_source != self.API_SOURCE_RADIODE:
            self._set_api_source(self.API_SOURCE_RADIODE)

    def _capture_radiode_station_id_from_payload(self, context, payload):
        ctx = str(context or '')
        if not ctx.startswith('radiode.'):
            return
        station_id = ''
        try:
            if isinstance(payload, list) and payload and isinstance(payload[0], dict):
                station_id = str(payload[0].get('stationId') or '').strip()
            elif isinstance(payload, dict):
                station_id = str(payload.get('stationId') or '').strip()
        except Exception:
            station_id = ''
        if not station_id:
            return
        if self.station_id != station_id:
            self.station_id = station_id
            log_debug(f"radio.de Station-ID aus API-RAW gesetzt: '{station_id}' (context={ctx})")
        if not self.plugin_slug:
            self.plugin_slug = station_id
        if self.api_source != self.API_SOURCE_RADIODE:
            self._set_api_source(self.API_SOURCE_RADIODE)

    def _compose_song_label(self, artist=None, title=None):
        a = (artist or '').strip()
        t = (title or '').strip()
        if a and t:
            return f"{a} - {t}"
        return t or a

    def _sanitize_station_text(self, value):
        """
        Entfernt Kodi-Markup (z.B. [COLOR ...][/COLOR]) und Bullet-Zeichen
        aus einem Stationslabel fuer externe Requests.
        """
        text = str(value or '')
        if not text:
            return ''
        text = re.sub(r'\[[^\]]+\]', '', text)
        text = text.replace('•', ' ')
        return ' '.join(text.split()).strip()

    def _set_api_nowplaying_label(self, artist=None, title=None):
        value = self._compose_song_label(artist, title)
        if value:
            self.set_property_safe(_P.API_NOW, value)
        else:
            WINDOW.clearProperty(_P.API_NOW)

    def _debug_log_api_raw(self, context, payload):
        """Zentrales Debug-Logging fuer API-Rohdaten."""
        try:
            text = str(payload)
        except Exception:
            text = repr(payload)
        if len(text) > 500:
            text = text[:500] + '...'
        log_debug(f"API-RAW[{context}]: {text}")
        self._capture_radiode_station_id_from_payload(context, payload)
        self.raw_sources.set_api_payload(context, payload)

    def _debug_log_raw_payload(self, context, payload):
        """Zentrales Debug-Logging fuer kompakte Rohdaten-Payloads."""
        try:
            text = str(payload)
        except Exception:
            text = repr(payload)
        if len(text) > 500:
            text = text[:500] + '...'
        log_debug(f"RAW[{context}]: {text}")

    def _set_icy_nowplaying_label(self, stream_title=None, artist=None, title=None):
        raw = (stream_title or '').strip()
        value = raw if raw else self._compose_song_label(artist, title)
        if value:
            self.set_property_safe(_P.ICY_NOW, value)
        else:
            WINDOW.clearProperty(_P.ICY_NOW)

    def _qf_response_snapshot(self):
        response_id = (WINDOW.getProperty(_P.QF_RESPONSE_ID) or '').strip()
        status = (WINDOW.getProperty(_P.QF_RESPONSE_STATUS) or '').strip().lower()
        artist = (WINDOW.getProperty(_P.QF_RESPONSE_ARTIST) or '').strip()
        title = (WINDOW.getProperty(_P.QF_RESPONSE_TITLE) or '').strip()
        fresh = bool(
            self._last_qf_request_id
            and response_id
            and response_id == self._last_qf_request_id
        )
        return {
            'fresh': fresh,
            'response_id': response_id,
            'status': status,
            'artist': artist,
            'title': title,
        }

    def _is_qf_fallback_exception(self):
        """
        Liefert True, wenn QF fuer den aktuellen Zyklus als nicht verfuegbar gilt
        und API/ICY/MP als Ausnahmefall einspringen duerfen.
        """
        if not self._qf_enabled or not self.is_playing:
            return True
        if not self._last_qf_request_id:
            return True

        snapshot = self._qf_response_snapshot()
        if snapshot.get('fresh'):
            # Harte Fehler -> Fallback erlaubt.
            return snapshot.get('status') in ('resolve_error', 'error', 'timeout')

        # Keine frische Response: erst nach Timeout Fallback erlauben.
        try:
            age_s = max(0.0, float(time.time() - float(self._last_qf_request_ts or 0.0)))
        except Exception:
            age_s = float(QF_NO_RESPONSE_FALLBACK_S)
        return age_s >= float(QF_NO_RESPONSE_FALLBACK_S)

    def _is_qf_authoritative(self):
        """
        QF ist autoritativ, solange kein expliziter Ausnahmefall vorliegt.
        Das gilt auch dann, wenn QF aktuell "kein Song" liefert.
        """
        return bool(self._qf_enabled and self.is_playing and not self._is_qf_fallback_exception())

    def _current_qf_hit_pair(self, invalid_values, require_fresh=True):
        """
        Liefert das aktuelle QF-Paar bei passender "hit"-Response.
        Standard: nur fresh responses (request_id match).
        Für aktiven ASM-QF-Lock kann require_fresh=False gesetzt werden, damit
        QF-Paarwechsel nicht an Request-Race-Conditions scheitern.
        """
        _ = invalid_values
        snapshot = self._qf_response_snapshot()
        if require_fresh and not snapshot.get('fresh'):
            return ('', '')
        if snapshot.get('status') != 'hit':
            return ('', '')
        artist = str(snapshot.get('artist') or '').strip()
        title = str(snapshot.get('title') or '').strip()
        if not (artist and title):
            return ('', '')
        return (artist, title)

    def _send_qf_request(self, station_name, mode='asm_auto'):
        station = self._sanitize_station_text(station_name)
        if not station:
            return False
        now_ts = time.time()
        self._qf_request_seq = (self._qf_request_seq + 1) % 1000000
        request_id = f"asm-{int(now_ts * 1000)}-{self._qf_request_seq}"
        WINDOW.setProperty(_P.QF_REQUEST_STATION, station)
        WINDOW.clearProperty(_P.QF_REQUEST_STATION_ID)
        WINDOW.setProperty(_P.QF_REQUEST_MODE, str(mode or 'asm_auto'))
        WINDOW.setProperty(_P.QF_REQUEST_TS, str(int(now_ts)))
        # Request-ID immer zuletzt setzen, damit ASM-QF ein konsistentes Request-Paket liest.
        WINDOW.setProperty(_P.QF_REQUEST_ID, request_id)
        self._last_qf_request_id = request_id
        self._last_qf_request_station = station
        self._last_qf_request_ts = now_ts
        log_debug(
            f"ASM-QF Request gesendet: id='{request_id}', station='{station}', "
            f"mode='{mode}'"
        )
        return True

    def _clear_qf_response_properties(self):
        WINDOW.clearProperty(_P.QF_RESPONSE_ID)
        WINDOW.clearProperty(_P.QF_RESPONSE_STATUS)
        WINDOW.clearProperty(_P.QF_RESPONSE_ARTIST)
        WINDOW.clearProperty(_P.QF_RESPONSE_TITLE)
        WINDOW.clearProperty(_P.QF_RESPONSE_SOURCE)
        WINDOW.clearProperty(_P.QF_RESPONSE_REASON)
        WINDOW.clearProperty(_P.QF_RESPONSE_META)
        WINDOW.clearProperty(_P.QF_RESPONSE_TS)

    def _tick_qf_request(self):
        if not self._qf_enabled or not self.is_playing:
            return
        if not self._has_addon(self.QF_SERVICE_ADDON_ID):
            self._ensure_qf_addon_installed()
            return

        station_name = self._sanitize_station_text(WINDOW.getProperty(_P.STATION) or '')
        if not station_name:
            return

        now_ts = time.time()
        station_changed = station_name.strip().lower() != self._last_qf_request_station.strip().lower()

        # Solange die letzte Anfrage noch unbeantwortet ist, keine neue Request-ID senden.
        # Sonst "ueberholt" der naechste Request die laufende Antwort und die Rueckmeldung
        # wird wegen ID-Mismatch als non-fresh verworfen.
        if self._last_qf_request_id and not station_changed:
            snapshot = self._qf_response_snapshot()
            if not snapshot.get('fresh'):
                request_age_s = max(0.0, float(now_ts - float(self._last_qf_request_ts or 0.0)))
                if request_age_s < float(QF_NO_RESPONSE_FALLBACK_S):
                    return

        request_due = (
            not self._last_qf_request_id
            or station_changed
            or (
                now_ts - (
                    self._last_qf_response_match_ts
                    if (
                        self._last_qf_response_match_ts > 0.0
                        and self._last_qf_response_id
                        and self._last_qf_response_id == self._last_qf_request_id
                    )
                    else self._last_qf_request_ts
                )
            ) >= API_NOW_REFRESH_INTERVAL_S
        )
        if not request_due:
            return

        if station_changed:
            self._last_qf_result = ''
            self._clear_qf_response_properties()
            WINDOW.clearProperty(_P.QF_RESULT)
        self._send_qf_request(station_name=station_name, mode='asm_auto')

    def _sync_qf_result_property(self):
        """
        Spiegelt das ASM-QF Song-Ergebnis in ein ASM-Label fuer Skins.
        Ziel-Property: RadioMonitor.QF.Result
        """
        if not self._qf_enabled or not self.is_playing:
            self._last_qf_result = ''
            if not self.is_playing:
                self._clear_qf_response_properties()
            WINDOW.clearProperty(_P.QF_RESULT)
            return

        if not self._last_qf_request_id:
            self._last_qf_result = ''
            WINDOW.clearProperty(_P.QF_RESULT)
            return

        snapshot = self._qf_response_snapshot()
        qf_authoritative = self._is_qf_authoritative()
        if not snapshot.get('fresh'):
            return
        response_id = snapshot.get('response_id') or ''
        if response_id and response_id != self._last_qf_response_id:
            self._last_qf_response_id = response_id
            self._last_qf_response_match_ts = time.time()

        if snapshot.get('status') != 'hit':
            self._last_qf_result = ''
            WINDOW.clearProperty(_P.QF_RESULT)
            # QF bleibt in diesem Zustand autoritativ (z.B. "kein Song"):
            # Song-Labels aktiv leeren, damit kein alter Song stehen bleibt.
            if qf_authoritative:
                WINDOW.clearProperty(_P.ARTIST)
                WINDOW.clearProperty(_P.ARTIST_DISPLAY)
                WINDOW.clearProperty(_P.TITLE)
            return
        artist = str(snapshot.get('artist') or '').strip()
        title = str(snapshot.get('title') or '').strip()
        if not (artist and title):
            self._last_qf_result = ''
            WINDOW.clearProperty(_P.QF_RESULT)
            if qf_authoritative:
                WINDOW.clearProperty(_P.ARTIST)
                WINDOW.clearProperty(_P.ARTIST_DISPLAY)
                WINDOW.clearProperty(_P.TITLE)
            return

        label = self._compose_song_label(artist=artist, title=title)
        if not label:
            self._last_qf_result = ''
            WINDOW.clearProperty(_P.QF_RESULT)
            if qf_authoritative:
                WINDOW.clearProperty(_P.ARTIST)
                WINDOW.clearProperty(_P.ARTIST_DISPLAY)
                WINDOW.clearProperty(_P.TITLE)
            return

        if label != self._last_qf_result:
            self._last_qf_result = label
            self.set_property_safe(_P.QF_RESULT, label)
            # Im Exklusiv-Modus werden Artist/Title Labels sofort mit den 
            # ASM-QF Daten vorbefüllt (Poll-Feedback), noch bevor MB entscheidet.
            if self._qf_enabled:
                self.set_property_safe(_P.ARTIST, artist)
                # Viele Skins rendern ArtistDisplay statt Artist.
                # Daher QF-Prefill konsistent auf beide Properties schreiben.
                self.set_property_safe(_P.ARTIST_DISPLAY, artist)
                self.set_property_safe(_P.TITLE, title)

    def _capture_stream_url_raw(self, stream_url):
        value = (stream_url or '').strip()
        self.raw_sources.set_text(_P.RAW_STREAM_URL, value, max_len=2000)

    def _capture_plugin_playback_raw(self, playback_url):
        self.raw_sources.set_text(_P.RAW_PLUGIN_URL, (playback_url or '').strip(), max_len=2000)

    def _capture_listitem_raw(self, context=''):
        try:
            payload = {
                'label': xbmc.getInfoLabel('ListItem.Label') or '',
                'title': xbmc.getInfoLabel('ListItem.Title') or '',
                'artist': xbmc.getInfoLabel('ListItem.Artist') or '',
                'album': xbmc.getInfoLabel('ListItem.Album') or '',
                'path': xbmc.getInfoLabel('ListItem.Path') or '',
                'filenameandpath': xbmc.getInfoLabel('ListItem.FilenameAndPath') or '',
                'icon': xbmc.getInfoLabel('ListItem.Icon') or '',
                'thumb': xbmc.getInfoLabel('ListItem.Thumb') or '',
            }
            self.raw_sources.set_json(_P.RAW_LISTITEM, payload, max_len=12000)
            ctx = context or 'capture'
            self._debug_log_raw_payload(f"listitem.{ctx}", payload)
        except Exception as e:
            log_debug(f"Fehler beim Erfassen ListItem-Rohdaten: {e}")

    def _capture_playing_item_raw(self):
        try:
            item = self.player.getPlayingItem()
            item_data = snapshot_getters(item)
            self.raw_sources.set_json(_P.RAW_PLAYING_ITEM, item_data, max_len=12000)
        except Exception as e:
            log_debug(f"Fehler beim Erfassen PlayingItem-Rohdaten: {e}")

    def _capture_jsonrpc_player_raw(self):
        try:
            active_query = {
                "jsonrpc": "2.0",
                "method": "Player.GetActivePlayers",
                "id": 1
            }
            active_raw = xbmc.executeJSONRPC(json.dumps(active_query))
            active_data = json.loads(active_raw or '{}')
            players = active_data.get('result') or []
            if not players:
                self.raw_sources.set_text(_P.RAW_JSONRPC_PLAYER, '')
                return
            player_id = players[0].get('playerid')
            if player_id is None:
                self.raw_sources.set_text(_P.RAW_JSONRPC_PLAYER, '')
                return

            item_query = {
                "jsonrpc": "2.0",
                "method": "Player.GetItem",
                "params": {
                    "playerid": player_id,
                    "properties": [
                        "title", "artist", "album", "genre", "file",
                        "thumbnail", "fanart", "comment", "duration", "displayartist"
                    ]
                },
                "id": 2
            }
            item_raw = xbmc.executeJSONRPC(json.dumps(item_query))
            item_data = json.loads(item_raw or '{}')
            payload = {
                "active_players": players,
                "item": item_data.get('result', {})
            }
            self.raw_sources.set_json(_P.RAW_JSONRPC_PLAYER, payload, max_len=16000)
        except Exception as e:
            log_debug(f"Fehler beim Erfassen JSON-RPC-Rohdaten: {e}")

    def _emit_analysis_event(
        self,
        station_name='',
        stream_title='',
        trigger_reason='',
        decision_source='',
        decision_pair=('', ''),
        current_api_pair=('', ''),
        current_icy_pair=('', ''),
        current_mp_pair=('', ''),
        source_changed=False,
        note='',
    ):
        if not self.analysis_enabled or self.analysis_store is None:
            return
        try:
            self._analysis_seq += 1
            trace_id = new_trace_id()
            winner_artist = (decision_pair[0] if decision_pair else '') or ''
            winner_title = (decision_pair[1] if decision_pair else '') or ''
            winner_pair_label = f"{winner_artist} - {winner_title}".strip(' -')
            raw_listitem = WINDOW.getProperty(_P.RAW_LISTITEM) or ''
            raw_playing_item = WINDOW.getProperty(_P.RAW_PLAYING_ITEM) or ''
            raw_jsonrpc = WINDOW.getProperty(_P.RAW_JSONRPC_PLAYER) or ''
            listitem_pair = _extract_listitem_pair(raw_listitem)
            playing_item_pair = _extract_playing_item_pair(raw_playing_item)
            jsonrpc_pair = _extract_jsonrpc_pair(raw_jsonrpc)
            event = {
                'seq': self._analysis_seq,
                'ts': round(time.time(), 3),
                'trace_id': trace_id,
                'station': station_name or '',
                'stream_title': stream_title or '',
                'trigger_reason': trigger_reason or '',
                'source_changed': bool(source_changed),
                'decision': {
                    'source': decision_source or '',
                    'artist': winner_artist,
                    'title': winner_title,
                },
                'candidates': {
                    'api': list(current_api_pair or ('', '')),
                    'icy': list(current_icy_pair or ('', '')),
                    'listitem': list(listitem_pair or ('', '')),
                    'playing_item': list(playing_item_pair or ('', '')),
                    'jsonrpc': list(jsonrpc_pair or ('', '')),
                    'asm-qf': list(self._last_policy_context.get('current_qf_pair') or ('', '')),
                },
                'policy': dict(self._last_policy_context or {}),
                'source_hints': self._get_station_profile_hints(station_name),
                'raw_labels': {
                    'stream_url': WINDOW.getProperty(_P.RAW_STREAM_URL) or '',
                    'plugin_url': WINDOW.getProperty(_P.RAW_PLUGIN_URL) or '',
                    'icy_raw': WINDOW.getProperty(_P.RAW_ICY_METADATA) or '',
                },
                'note': note or '',
            }
            self.analysis_store.add_event(event)
            self.raw_sources.set_text(_P.AN_TRACE_ID, trace_id, max_len=64)
            self.raw_sources.set_text(_P.AN_TRIGGER, trigger_reason or note or '', max_len=256)
            self.raw_sources.set_text(_P.AN_WINNER_SOURCE, decision_source or '', max_len=64)
            self.raw_sources.set_text(_P.AN_WINNER_PAIR, winner_pair_label, max_len=512)
            self.raw_sources.set_json(_P.AN_LAST_EVENT, event, max_len=16000)
        except Exception as e:
            log_debug(f"Fehler beim Schreiben Analyse-Event: {e}")
    
    def update_player_metadata(self, artist, title, album_or_station, logo=None, mbid=None):
        """Versucht die Kodi Player Metadaten zu aktualisieren (für Standard InfoLabels)"""
        try:
            if not self.player.isPlayingAudio():
                return
            
            # Erstelle ein ListItem mit den korrekten Metadaten
            list_item = xbmcgui.ListItem()
            
            # Setze MusicInfoTag
            info_tag = list_item.getMusicInfoTag()
            if title:
                info_tag.setTitle(title)
            if artist:
                info_tag.setArtist(artist)
            if album_or_station:
                info_tag.setAlbum(album_or_station)  # Album oder Station als Fallback
            if mbid:
                # Kodi/Python API unterscheidet je nach Version bei Methodennamen und Parametertyp.
                set_mbid_methods = [
                    ('setMusicBrainzArtistID', [mbid]),
                    ('setMusicBrainzArtistID', mbid),
                    ('setMusicBrainzArtistId', [mbid]),
                    ('setMusicBrainzArtistId', mbid),
                ]
                for method_name, arg in set_mbid_methods:
                    method = getattr(info_tag, method_name, None)
                    if callable(method):
                        try:
                            method(arg)
                            log_debug(f"Player MBID gesetzt über {method_name}: {mbid}")
                            break
                        except Exception:
                            continue
            
            # Setze Logo als Cover Art
            if logo and logo != "DefaultAudio.png":
                list_item.setArt({'thumb': logo, 'poster': logo, 'icon': logo})
            
            # Versuche den Player zu aktualisieren (klappt möglicherweise nicht bei allen Kodi Versionen)
            # Dies ist ein "Best Effort" - es kann sein, dass es nicht funktioniert
            try:
                # Diese Methode existiert ab Kodi 18+
                self.player.updateInfoTag(list_item)
                log_debug(f"Player InfoTag aktualisiert: {artist} - {title}")
            except AttributeError:
                # Fallback: Setze Properties, die Skins nutzen können
                log_debug(f"updateInfoTag() nicht verfügbar - nutze nur Window Properties")
            
        except Exception as e:
            log_debug(f"Fehler beim Aktualisieren der Player Metadaten: {str(e)}")
            
    def _setup_api_fallback_from_url(self, url):
        """
        Versucht den Stationsnamen aus der Stream-URL zu extrahieren und setzt
        das API-Fallback-Flag, wenn kein icy-metaint Header verfügbar ist.
        Wird aufgerufen wenn der Stream keine ICY-Metadaten liefert.
        """
        self._ensure_api_source_from_context(url, 'setup_api_fallback_from_url')
        self._reconcile_api_source('setup_api_fallback_from_url')
        if not self._is_api_source_allowed():
            self._log_api_source_blocked('setup_api_fallback_from_url')
            return None

        try:
            if self._can_use_radiode_api():
                self.use_api_fallback = True
                log_debug("radio.de Stream erkannt, versuche Stationsnamen aus URL")

                match = re.search(r'stream\.([^/]+)\.de/([^/]+)', url)
                if not match:
                    match = re.search(r'//([^/]+)/([^/]+)', url)

                if match:
                    station_slug = match.group(2)
                    station_name = station_slug.replace('-', ' ').replace('_', ' ').title()

                    # Bekannte Sonderfälle normalisieren
                    station_name = station_name.replace('Brf ', 'Berliner Rundfunk ')
                    station_name = station_name.replace('100prozent', '100%')

                    self.set_property_safe(_P.STATION, station_name)
                    log_debug(f"Station aus URL erkannt: {station_name}")

                    self.station_slug = station_slug
                    self._record_verified_station_source(
                        station_name=station_name,
                        stream_url=url,
                        source_kind='asm_url_pattern',
                        confidence=0.60,
                    )

                    return station_name

            if self._can_use_tunein_api():
                self.use_api_fallback = True
                tunein_id = _tunein_extract_station_id(url)
                if tunein_id:
                    self.tunein_station_id = tunein_id
                    log_debug(f"TuneIn Stream erkannt, Station-ID aus URL: '{tunein_id}'")
                else:
                    log_debug("TuneIn Stream erkannt, aber keine Station-ID in URL gefunden")
        except Exception as e:
            log_debug(f"Fehler bei URL-Analyse fuer API-Fallback: {str(e)}")
        return None

    def get_tunein_api_nowplaying(self, station_name=None):
        """Delegiert an tunein-Modul."""
        return _tunein_get_nowplaying(
            self.api_client,
            self.tunein_station_id,
            station_name,
            debug_log=self._debug_log_api_raw
        )

    def _refresh_api_nowplaying_property(self, station_name=None, force=False):
        """
        Aktualisiert RadioMonitor.ApiNowPlaying periodisch aus der aktiven API-Quelle.
        Nutzt bewusst keinen MusicPlayer-Fallback.
        """
        self._reconcile_api_source('_refresh_api_nowplaying_property')
        if not self._is_api_source_allowed():
            WINDOW.clearProperty(_P.API_NOW)
            self._latest_api_pair = ('', '')
            return

        now_ts = time.time()
        if not force and (now_ts - self._last_api_now_refresh_ts) < API_NOW_REFRESH_INTERVAL_S:
            return
        self._last_api_now_refresh_ts = now_ts

        s_name = station_name or WINDOW.getProperty(_P.STATION) or ''
        artist, title = None, None

        if self._can_use_radiode_api() and (s_name or self.plugin_slug):
            artist, title = self.get_radiode_api_nowplaying(s_name)
        elif self._can_use_tunein_api():
            artist, title = self.get_tunein_api_nowplaying(s_name)

        if artist or title:
            invalid_values = INVALID_METADATA_VALUES + ['', s_name]
            n_artist, n_title = self._normalize_song_candidate(artist, title, invalid_values)
            if n_artist and n_title:
                self._latest_api_pair = (n_artist, n_title)
                self._set_api_nowplaying_label(n_artist, n_title)
            else:
                self._latest_api_pair = ('', '')
                self._set_api_nowplaying_label(artist, title)
        else:
            WINDOW.clearProperty(_P.API_NOW)
            self._latest_api_pair = ('', '')
    
    def get_nowplaying_from_apis(self, station_name, stream_url):
        """Versucht nowPlaying von verschiedenen APIs zu holen"""
        self._ensure_api_source_from_context(stream_url, 'get_nowplaying_from_apis')
        self._reconcile_api_source('get_nowplaying_from_apis')
        if not self._is_api_source_allowed():
            self._log_api_source_blocked('get_nowplaying_from_apis')
            WINDOW.clearProperty(_P.API_NOW)
            return None, None

            log_debug(f"API-Fallback gestartet für Station: '{station_name}'")

        # 1. radio.de API nur wenn Source=radio.de
        if self._can_use_radiode_api() and (station_name or self.plugin_slug):
            artist, title = self.get_radiode_api_nowplaying(station_name)
            if artist or title:
                log_info(f"OK radio.de API: {artist} - {title}")
                self._set_api_nowplaying_label(artist, title)
                return artist, title

        # 2. TuneIn API nur wenn Source=TuneIn
        if self._can_use_tunein_api():
            artist, title = self.get_tunein_api_nowplaying(station_name)
            if artist or title:
                log_info(f"OK TuneIn API: {artist} - {title}")
                self._set_api_nowplaying_label(artist, title)
                return artist, title
        # 3. Optionaler Fallback: Kodi Player InfoTags (nur mit MP-Entscheidung)
        WINDOW.clearProperty(_P.API_NOW)
        if not self._is_mp_decision_active():
            return None, None
        try:
            if self.player.isPlayingAudio():
                info_tag = self.player.getMusicInfoTag()
                title = info_tag.getTitle()
                artist = info_tag.getArtist()

                invalid_values = INVALID_METADATA_VALUES + ['', station_name]
                if title and title not in invalid_values:
                    # Filter Zahlen-IDs
                    if _NUMERIC_ID_RE.match(title):
                        log_debug(f"Player InfoTag enthaelt Zahlen-ID, ignoriere: {title}")
                        return None, None

                    # Filter einzelne Zahlen als Artist
                    if artist and re.match(r'^\d+$', artist):
                        log_debug(f"Player InfoTag Artist ist nur eine Zahl, ignoriere: {artist}")
                        artist = None

                    # Filter einzelne Zahlen als Title
                    if title and re.match(r'^\d+$', title):
                        log_debug(f"Player InfoTag Title ist nur eine Zahl, ignoriere: {title}")
                        return None, None

                    # Filter bekannte Platzhalter bei Artist
                    if artist and artist in invalid_values:
                        artist = None

                    # Wenn Artist valide ist
                    if artist:
                        return artist, title
                    else:
                        # Versuche zu parsen
                        parsed_artist, parsed_title = self.parse_stream_title_simple(title)
                        if parsed_artist and parsed_title:
                            return parsed_artist, parsed_title
                        return None, title
        except Exception as e:
            log_debug(f"Fehler beim Lesen Player InfoTags: {str(e)}")
        
        return None, None
    
    def parse_stream_title_simple(self, stream_title):
        """Einfache Trennung ohne API-Aufrufe (Nutzt zentrales metadata Modul)"""
        return _parse_stream_title_simple(stream_title)

    def _normalize_song_candidate(self, artist, title, invalid_values):
        """
        Normalisiert und validiert einen Kandidaten.
        Rückgabe: (artist, title) oder (None, None), wenn nicht verwertbar.
        """
        a = (artist or '').strip()
        t = (title or '').strip()
        if not a or not t:
            return None, None
        if a in invalid_values or t in invalid_values:
            return None, None
        # ID-Rauschen wie "277353 - 386535" nicht als Song werten.
        if re.match(r'^\d{3,}$', a) and re.match(r'^\d{3,}$', t):
            return None, None
        if _NUMERIC_ID_RE.match(a) or _NUMERIC_ID_RE.match(t):
            return None, None
        return a, t

    def _is_dot_sensitive_artist_conflict(self, source_artist, mb_artist):
        """
        Erkennt Konflikte wie 'Haven.' vs 'Haven'.
        In solchen Fällen darf MB den Artist/MBID nicht übernehmen, da der
        Punkt bei manchen Künstlernamen semantisch relevant ist.
        """
        src = str(source_artist or '').strip()
        mb = str(mb_artist or '').strip()
        if not src or not mb:
            return False
        if src.casefold() == mb.casefold():
            return False

        src_has_dot = src.endswith('.')
        mb_has_dot = mb.endswith('.')
        if src_has_dot == mb_has_dot:
            return False

        src_base = re.sub(r'\.+$', '', src).strip().casefold()
        mb_base = re.sub(r'\.+$', '', mb).strip().casefold()
        return bool(src_base and mb_base and src_base == mb_base)

    def _read_musicplayer_candidates(self, invalid_values):
        """
        Liest MusicPlayer-Kandidaten (direkt + swapped) zentral aus.
        Rückgabe: (mp_direct, mp_swapped)
        """
        mp_direct = (None, None)
        mp_swapped = (None, None)
        try:
            if self.player.isPlayingAudio():
                info_tag = self.player.getMusicInfoTag()
                tag_artist_raw = info_tag.getArtist()
                tag_title_raw = info_tag.getTitle()
                label_artist_raw = xbmc.getInfoLabel('MusicPlayer.Artist')
                label_title_raw = xbmc.getInfoLabel('MusicPlayer.Title')

                tag_pair = self._normalize_song_candidate(tag_artist_raw, tag_title_raw, invalid_values)
                label_pair = self._normalize_song_candidate(label_artist_raw, label_title_raw, invalid_values)

                if label_pair[0] and label_pair[1]:
                    if tag_pair != label_pair:
                        log_info(
                            f"MusicPlayer-Kandidat via InfoLabel bevorzugt: "
                            f"'{label_pair[0]} - {label_pair[1]}'")
                    mp_artist, mp_title = label_pair
                else:
                    mp_artist, mp_title = tag_pair

                if mp_artist and mp_title:
                    mp_direct = (mp_artist, mp_title)
                    if mp_artist != mp_title:
                        s_mp_artist, s_mp_title = self._normalize_song_candidate(mp_title, mp_artist, invalid_values)
                        if s_mp_artist and s_mp_title and (s_mp_artist, s_mp_title) != mp_direct:
                            mp_swapped = (s_mp_artist, s_mp_title)
        except Exception as e:
            log_debug(f"Fehler beim Lesen MusicPlayer Kandidat: {str(e)}")
        return mp_direct, mp_swapped

    def _valid_song_pairs(self, *pairs):
        """Filtert nur valide (artist, title)-Paare."""
        return [p for p in pairs if p and p[0] and p[1]]

    def _select_musicplayer_pair_for_source(self, source, mp_pairs, last_winner_pair=None):
        """
        Waehlt das passende MP-Paar fuer die aktuelle MP-Quellenvariante:
        - musicplayer -> direktes Paar
        - musicplayer_swapped -> swapped Paar (falls vorhanden)
        - Bei fremder Winner-Quelle (api/icy): orientiert bevorzugt auf das Paar,
          das zum letzten Winner passt, damit MP nicht kuenstlich als "other song"
          gewertet wird (wichtig fuer swapped-Feeds).
        """
        if not mp_pairs:
            return ('', '')
        src = str(source or '')
        if src.startswith('musicplayer_swapped') and len(mp_pairs) > 1:
            return mp_pairs[1]
        if not src.startswith('musicplayer') and len(mp_pairs) > 1:
            winner_pair = last_winner_pair or ('', '')
            if winner_pair and winner_pair[0] and winner_pair[1]:
                if mp_pairs[0] == winner_pair:
                    return mp_pairs[0]
                if mp_pairs[1] == winner_pair:
                    return mp_pairs[1]
            if src.endswith('_swapped'):
                return mp_pairs[1]
        return mp_pairs[0]

    def _is_player_buffering(self):
        """True solange Kodi den Stream noch puffert/laedt."""
        try:
            return bool(
                xbmc.getCondVisibility('Player.Caching')
                or xbmc.getCondVisibility('Window.IsActive(busydialog)')
                or xbmc.getCondVisibility('Window.IsActive(busydialognocancel)')
            )
        except Exception:
            return False

    def _wait_for_stable_playback_start(self, generation):
        """
        Wartet auf stabilen Playback-Start:
        - PLAYER_BUFFER_SETTLE_S lang ohne Buffering
        - Safety-Net nach PLAYER_BUFFER_MAX_WAIT_S
        """
        stable_since = None
        wait_started = time.time()
        logged_buffering = False
        logged_settle_wait = False

        while (
            not self.stop_thread
            and self.is_playing
            and generation == self.metadata_generation
        ):
            if self._is_player_buffering():
                stable_since = None
                if not logged_buffering:
                    log_debug(
                        f"Quellenfestlegung ausgesetzt: Player puffert noch")
                    logged_buffering = True
                logged_settle_wait = False
            else:
                if stable_since is None:
                    stable_since = time.time()
                    if not logged_settle_wait:
                        log_debug(
                            f"Quellenfestlegung wartet auf stabilen Start "
                            f"({PLAYER_BUFFER_SETTLE_S:.1f}s ohne Buffering)")
                        logged_settle_wait = True
                    logged_buffering = False
                elif (time.time() - stable_since) >= PLAYER_BUFFER_SETTLE_S:
                    return True

            if (time.time() - wait_started) >= PLAYER_BUFFER_MAX_WAIT_S:
                log_warning(
                    f"Buffering-Check Timeout nach {PLAYER_BUFFER_MAX_WAIT_S:.0f}s - "
                    f"setze Quellenfestlegung fort"
                )
                return True

            xbmc.sleep(100)

        return False

    def _determine_source_change_trigger(
        self,
        last_winner_source,
        last_winner_pair,
        current_mp_pair,
        current_api_pair,
        current_icy_pair,
        station_name,
        stream_title_changed,
        initial_source_pending,
        current_qf_pair=None
    ):
        """
        Zentrale Trigger-Erkennung ueber das modulare Source-Policy-Modell.
        """
        reasons = {
            'title': self.TRIGGER_TITLE_CHANGE,
            'asm-qf': self.TRIGGER_QF_CHANGE,
            'api': self.TRIGGER_API_CHANGE,
            'musicplayer': self.TRIGGER_MP_CHANGE,
            'mp_invalid': self.TRIGGER_MP_INVALID,
            'icy': self.TRIGGER_TITLE_CHANGE,
            'icy_stale': self.TRIGGER_ICY_STALE
        }
        self._refresh_station_profile_context(
            station_name,
            enable_policy=self._station_profile_policy_enabled
        )
        changed, reason, preferred = self.source_policy.decide_trigger(
            last_winner_source=last_winner_source,
            last_winner_pair=last_winner_pair,
            current_mp_pair=current_mp_pair,
            current_api_pair=current_api_pair,
            current_icy_pair=current_icy_pair,
            station_name=station_name,
            stream_title_changed=stream_title_changed,
            initial_source_pending=initial_source_pending,
            reasons=reasons,
            current_qf_pair=current_qf_pair
        )
        self._last_policy_context = {
            'station_name': station_name,
            'current_mp_pair': current_mp_pair if self._is_song_pair(current_mp_pair) else ('', ''),
            'current_api_pair': current_api_pair if self._is_song_pair(current_api_pair) else ('', ''),
            'current_icy_pair': current_icy_pair if self._is_song_pair(current_icy_pair) else ('', ''),
            'current_qf_pair': current_qf_pair if self._is_song_pair(current_qf_pair) else ('', ''),
            'stream_title_changed': bool(stream_title_changed),
            'triggered': bool(changed),
            'trigger_reason': reason if changed else ''
        }
        scores = self.source_policy.debug_scores()
        if not self._is_mp_decision_active() and isinstance(scores, dict):
            scores = {k: v for k, v in scores.items() if k != 'musicplayer'}
        self._policy_preferred_source = preferred or ''
        if changed or preferred != getattr(self, '_last_logged_preferred', None):
            self._last_logged_preferred = preferred
            log_debug(
                f"Source-Policy: scores={scores}, preferred='{preferred or 'none'}', "
                f"trigger='{reason if changed else 'none'}'"
            )
        return changed, reason

    def _resolve_stream_title_for_trigger(self, trigger_reason, stream_title, current_mp_pair):
        """
        Vereinheitlicht die StreamTitle-Basis fuer die Auswertung.
        Bei MP-Wechsel wird der Titel direkt aus MP gebildet.
        """
        if (
            trigger_reason == self.TRIGGER_MP_CHANGE
            and current_mp_pair[0]
            and current_mp_pair[1]
        ):
            return f"{current_mp_pair[0]} - {current_mp_pair[1]}"
        return stream_title

    def _source_family(self, source):
        """Normalisiert eine Source auf ihre Familie (asm-qf/api/icy/musicplayer)."""
        s = str(source or '')
        if s.startswith('asm-qf'):
            return 'asm-qf'
        if s.startswith('musicplayer'):
            return 'musicplayer'
        if s.startswith('icy'):
            return 'icy'
        if s.startswith('api'):
            return 'api'
        return s or 'other'

    def _split_source_candidates(self, candidates):
        """Teilt Kandidaten in Stream-Quelle (api/icy) und MusicPlayer."""
        stream_candidates = []
        mp_candidates = []
        for c in candidates or []:
            family = self._source_family(c.get('source'))
            if family in self.STREAM_SOURCE_FAMILIES:
                stream_candidates.append(c)
            elif family == self.MP_SOURCE_FAMILY:
                mp_candidates.append(c)
        return stream_candidates, mp_candidates

    def _log_musicplayer_comparison(self, decision_source, decision_pair, mp_pairs):
        """Loggt den Abgleich einer finalen Entscheidung gegen aktuelle MP-Paare."""
        src = str(decision_source or '')
        pair = decision_pair if decision_pair and decision_pair[0] and decision_pair[1] else ('', '')
        if not (src and pair[0] and pair[1]):
            return
        if not mp_pairs:
            log_debug(
                f"MP-Abgleich: keine MP-Kandidaten "
                f"(source={src}, pair='{pair[0]} - {pair[1]}')"
            )
            return
        if pair in set(mp_pairs):
            log_debug(
                f"MP-Abgleich: MATCH "
                f"(source={src}, pair='{pair[0]} - {pair[1]}')"
            )
        else:
            log_debug(
                f"MP-Abgleich: MISMATCH "
                f"(source={src}, pair='{pair[0]} - {pair[1]}')"
            )

    def _pair_for_source(self, source, direct_pair):
        """
        Liefert ein Paar in der Orientierung der angegebenen Source.
        Beispiel: source='api_swapped' -> (title, artist) wird zu (artist, title).
        """
        a = direct_pair[0] if direct_pair else ''
        b = direct_pair[1] if direct_pair else ''
        if not (a and b):
            return ('', '')
        s = str(source or '')
        if s.endswith('_swapped'):
            return (b, a)
        return (a, b)

    def _apply_locked_source_policy(self, candidates, locked_source, api_candidate, icy_pairs, mp_pairs):
        """
        Erzwingt Source-Lock fuer die aktuelle Auswertung:
        - Solange die gelockte Quelle valide Daten liefert, werden nur deren Kandidaten benutzt.
        - Erst wenn die gelockte Quelle keine validen Daten liefert, wird auf alle Kandidaten geoeffnet.
        """
        locked_family = self._source_family(locked_source)
        if locked_family not in ('asm-qf', 'musicplayer', 'api', 'icy'):
            return candidates

        has_locked_data = False
        if locked_family == 'asm-qf':
            has_locked_data = bool(self._last_policy_context.get('current_qf_pair'))
        elif locked_family == 'musicplayer':
            has_locked_data = bool(mp_pairs)
        elif locked_family == 'api':
            has_locked_data = bool(api_candidate and api_candidate[0] and api_candidate[1])
        elif locked_family == 'icy':
            has_locked_data = bool(icy_pairs)

        if not has_locked_data:
            log_info(f"Source-Lock geloest: '{locked_family}' ohne valide Daten -> Fallback aktiv")
            return candidates

        locked_candidates = [
            c for c in candidates
            if self._source_family(c.get('source')) == locked_family
        ]
        if locked_candidates:
            log_debug(
                f"Source-Lock aktiv: '{locked_family}' "
                f"(candidates={len(locked_candidates)})")
            return locked_candidates
        return candidates

    def _resolve_mb_zero_with_source_lock(self, locked_source, mp_pairs, api_candidate, icy_candidates):
        """
        MB=0 Fallback fuer source-locked Auswertungen:
        - Bei aktivem Lock darf keine andere Quelle den Lock uebersteuern.
        Rueckgabe: (source_name, (artist, title)) oder ('', ('', '')).
        """
        locked_source_name = str(locked_source or '')
        locked_family = self._source_family(locked_source_name)
        if locked_family == 'asm-qf':
            pair = self._last_policy_context.get('current_qf_pair')
            if pair and pair[0] and pair[1]:
                return locked_source_name, pair
        if locked_family == 'musicplayer' and mp_pairs:
            pair = self._select_musicplayer_pair_for_source(locked_source_name, mp_pairs)
            if pair and pair[0] and pair[1]:
                return locked_source_name, pair
        if (
            locked_family == 'api'
            and api_candidate
            and api_candidate[0]
            and api_candidate[1]
        ):
            return locked_source_name, self._pair_for_source(locked_source_name, api_candidate)
        if locked_family == 'icy' and icy_candidates:
            normalized = []
            for item in list(icy_candidates or []):
                if not isinstance(item, (list, tuple)) or len(item) != 2:
                    continue
                src_name = str(item[0] or '')
                pair = item[1] if isinstance(item[1], (list, tuple)) else ('', '')
                if pair and len(pair) == 2 and pair[0] and pair[1]:
                    normalized.append((src_name, (pair[0], pair[1])))
            if not normalized:
                return '', ('', '')

            prefer_swapped = (
                (self._prefer_icy_swapped_from_history() or self._effective_icy_format_hint() == 'title_artist')
                and not locked_source_name.endswith('_swapped')
            )
            preferred_sources = []
            if locked_source_name:
                preferred_sources.append(locked_source_name)
            if prefer_swapped:
                preferred_sources.extend(['icy_swapped', 'icy'])
            else:
                preferred_sources.extend(['icy', 'icy_swapped'])

            for preferred in preferred_sources:
                for src_name, pair in normalized:
                    if src_name == preferred:
                        return preferred, pair
            return normalized[0][0], normalized[0][1]
        return '', ('', '')

    def _apply_api_stale_override(self, candidates, trigger_reason, api_candidate, icy_pairs):
        """
        Spezialfall fuer API-stale Trigger:
        - Wenn ICY einen Kandidaten hat und Trigger in den API-stale Faellen,
          werden API-Kandidaten ausgeblendet.
        """
        _ = api_candidate
        if trigger_reason not in ('MusicPlayer-Wechsel (API stale)', self.TRIGGER_ICY_STALE) or not icy_pairs:
            return candidates

        filtered = []
        removed = 0
        for c in candidates:
            family = self._source_family(c.get('source'))
            drop = (family == 'api')
            if drop:
                removed += 1
            else:
                filtered.append(c)

        if filtered and removed > 0:
            log_debug(
                f"API-stale Override aktiv: {removed} Kandidaten ausgeblendet "
                f"(trigger={trigger_reason})")
            return filtered
        return candidates

    def _set_last_song_decision(self, source, artist=None, title=None):
        """Speichert die letzte Gewinnerquelle zentral fuer source-locked Trigger."""
        self._last_decision_source = str(source or '')
        source_family = self._source_family(self._last_decision_source)
        if source_family in ('asm-qf', 'musicplayer', 'icy', 'api'):
            self.set_property_safe(_P.SOURCE, source_family)
            self.set_property_safe(_P.SOURCE_DETAIL, self._last_decision_source)
            log_debug(
                f"Quellenentscheidung final: source='{source_family}', "
                f"detail='{self._last_decision_source}', pair='{artist or ''} - {title or ''}'"
            )
        else:
            WINDOW.clearProperty(_P.SOURCE)
            WINDOW.clearProperty(_P.SOURCE_DETAIL)
        if artist and title:
            self._last_decision_pair = (artist, title)
        else:
            self._last_decision_pair = ('', '')

    def _is_musicplayer_trusted(self):
        """MusicPlayer ist nur innerhalb der aktuellen Metadata-Generation vertrauenswürdig."""
        return self.musicplayer_trust.is_trusted(self.metadata_generation)

    def _register_musicplayer_mismatch(self, reason=''):
        """Registriert einen Trust-Fehler (z. B. fehlende/ungueltige MusicPlayer-Daten)."""
        self.musicplayer_trust.register_mismatch(self.metadata_generation, reason=reason)

    def _update_musicplayer_trust_after_decision(self, decision_source, decision_pair, mp_pairs):
        """
        Aktualisiert den MusicPlayer-Trust nach einer finalen Song-Entscheidung.
        Regel:
        - Trust wird nur dann aufgebaut, wenn eine externe Entscheidung (api/icy) den MP bestätigt.
        - Im MP-master Modus wird bei externem Widerspruch nicht automatisch de-vertraut.
        """
        self.musicplayer_trust.update_after_decision(
            self.metadata_generation,
            decision_source,
            decision_pair,
            mp_pairs
        )

    def _should_use_musicplayer_candidates(self, mp_pairs, api_candidate, icy_pairs, station_name=''):
        """
        Steuert zentral, ob MusicPlayer-Kandidaten in die MB-Wahl aufgenommen werden.
        Policy:
        - MusicPlayer wird nur mitbewertet, wenn mindestens ein nicht-generisches
          Song-Paar vorliegt (Senderinfos/Jingles auf MP werden ignoriert).
        """
        _ = api_candidate
        _ = icy_pairs
        return bool(self._filter_non_generic_song_pairs(mp_pairs, station_name))

    def _evaluate_mb_candidate(self, source, artist, title):
        """
        Bewertet einen Kandidaten via MusicBrainz.
        """
        if not self._is_pre_mb_song_pair((artist, title), source=source):
            log_debug(
                f"MB-Kandidat[{source}] uebersprungen "
                f"(pre_mb_policy): '{artist} - {title}'"
            )
            return {
                'source': source,
                'input_artist': artist,
                'input_title': title,
                'corrected_artist': artist,
                'corrected_title': title,
                'score': 0,
                'artist_sim': 0.0,
                'title_sim': 0.0,
                'combined': 0.0,
                'mb_artist': artist or '',
                'mb_title': title or '',
                'mb_album': '',
                'mb_album_date': '',
                'mbid': '',
                'mb_first_release': '',
                'mb_duration_ms': 0,
                'dot_conflict': False,
            }

        score, mb_artist, mb_title, mbid, mb_album, mb_album_date, mb_first_release, mb_duration_ms = \
            _musicbrainz_query_recording(title, artist)
        artist_sim = _mb_similarity(artist, mb_artist) if mb_artist else 0.0
        title_sim = _mb_similarity(title, mb_title) if mb_title else 0.0
        combined = float(score) * ((artist_sim + title_sim) / 2.0)
        dot_conflict = self._is_dot_sensitive_artist_conflict(artist, mb_artist)
        if dot_conflict:
            log_info(
                f"MB-Kandidat geblockt (Punkt-Konflikt): "
                f"source={source}, input_artist='{artist}', mb_artist='{mb_artist}'"
            )
            score = 0
            combined = 0.0
            mbid = ''
            mb_album = ''
            mb_album_date = ''
            mb_first_release = ''
            mb_duration_ms = 0
        # MB-Bereinigung: korrigierten Label nur uebernehmen wenn MB eindeutig
        # denselben Song bestaetigt (hohe Aehnlichkeit zu den Eingabewerten).
        # Verhindert, dass ein komplett anderer MB-Treffer die Labels ueberschreibt.
        if (mb_artist and mb_title
                and not dot_conflict
                and artist_sim >= _MB_LABEL_CORRECTION_MIN_SIM
                and title_sim >= _MB_LABEL_CORRECTION_MIN_SIM):
            corrected_artist = mb_artist
            corrected_title = mb_title
        else:
            corrected_artist = artist
            corrected_title = title
        return {
            'source': source,
            'input_artist': artist,
            'input_title': title,
            'corrected_artist': corrected_artist,
            'corrected_title': corrected_title,
            'score': int(score),
            'artist_sim': float(artist_sim),
            'title_sim': float(title_sim),
            'combined': float(combined),
            'mb_artist': mb_artist or artist,
            'mb_title': mb_title or title,
            'mb_album': mb_album or '',
            'mb_album_date': mb_album_date or '',
            'mbid': mbid or '',
            'mb_first_release': mb_first_release or '',
            'mb_duration_ms': int(mb_duration_ms or 0),
            'dot_conflict': bool(dot_conflict),
        }

    def _select_mb_winner(self, candidates):
        """
        Wählt den besten Song-Kandidaten per MB-Score.
        Rückgabe: (winner_dict|None, evaluations_list).
        """
        if not candidates:
            return None, []

        evaluations = []
        for c in candidates:
            ev = self._evaluate_mb_candidate(c['source'], c['artist'], c['title'])
            evaluations.append(ev)
            log_debug(
                f"MB-Kandidat[{ev['source']}]: "
                f"in='{ev['input_artist']} - {ev['input_title']}', "
                f"score={ev['score']}, artist_sim={ev['artist_sim']:.2f}, "
                f"title_sim={ev['title_sim']:.2f}, combined={ev['combined']:.1f}")

        valid = [
            ev for ev in evaluations
            if (ev['score'] >= self.MB_WINNER_MIN_SCORE and ev['combined'] >= self.MB_WINNER_MIN_COMBINED)
            or str(ev.get('source', '')).startswith('asm-qf')
        ]
        if not valid:
            log_debug(
                f"MB-Winner: kein Kandidat über Schwellwert "
                f"(min_score={self.MB_WINNER_MIN_SCORE}, min_combined={self.MB_WINNER_MIN_COMBINED:.1f})")
            return None, evaluations

        def _source_rank(source):
            s = str(source or '')
            if s.startswith('asm-qf'):
                return 3
            if s.startswith('musicplayer'):
                return 2
            if s.startswith('icy'):
                return 1
            if s.startswith('api'):
                return 0
            return 0

        trigger_reason = str(getattr(self, '_parse_trigger_reason', '') or '')
        prev_pair = getattr(self, '_parse_prev_winner_pair', ('', ''))
        prev_pair_valid = bool(prev_pair and prev_pair[0] and prev_pair[1])

        # Bei einem erkannten Wechsel-Trigger den vorherigen Winner aus der
        # aktuellen Auswahl entfernen, sobald es mindestens eine valide Alternative gibt.
        effective_valid = list(valid)
        if trigger_reason and prev_pair_valid:
            non_prev_valid = [
                ev for ev in effective_valid
                if (ev.get('input_artist'), ev.get('input_title')) != prev_pair
            ]
            if non_prev_valid:
                filtered_count = len(effective_valid) - len(non_prev_valid)
                effective_valid = non_prev_valid
                log_debug(
                    f"MB-Winner: Vorheriger Winner geblockt "
                    f"(Trigger={trigger_reason}, entfernt={filtered_count})")

        winner_pool = effective_valid

        # Dominanz-Regel fuer ASM-QF: wenn ASM-QF Daten liefert, ist dies
        # die vorrangige Auswahl, unabhängig von Konsens anderer Quellen.
        qf_candidates = [ev for ev in effective_valid if str(ev.get('source', '')).startswith('asm-qf')]
        if qf_candidates:
            winner_pool = qf_candidates
            log_debug(f"MB-Winner: ASM-QF Dominanz aktiv (candidates={len(qf_candidates)})")
        else:
            # Einheitliche Gewinnerregel:
            # - Bei Gleichstand entscheidet die Quellen-Prioritaet (MusicPlayer > ICY > API).
            # - Konsens-Paare (mind. zwei Quellenfamilien) bleiben bevorzugt.
            pair_support = {}
            for ev in effective_valid:
                pair = (ev.get('input_artist'), ev.get('input_title'))
                fam = self._source_family(ev.get('source'))
                pair_support.setdefault(pair, set()).add(fam)
            consensus_pairs = {
                pair for pair, families in pair_support.items() if len(families) >= 2
            }
            if consensus_pairs:
                consensus_pool = [
                    ev for ev in effective_valid
                    if (ev.get('input_artist'), ev.get('input_title')) in consensus_pairs
                ]
                if consensus_pool:
                    winner_pool = consensus_pool
                    log_debug(
                        f"MB-Winner: Konsens aktiv "
                        f"(pairs={len(consensus_pairs)}, candidates={len(consensus_pool)})")

        winner = max(
            winner_pool,
            key=lambda ev: (
                _source_rank(ev.get('source')),
                ev['combined'],
                ev['score']
            )
        )
        log_info(
            f"MB-Winner: source={winner['source']} "
            f"('{winner['input_artist']} - {winner['input_title']}'), "
            f"score={winner['score']}, combined={winner['combined']:.1f}"
        )
        return winner, evaluations
    
    def get_radiode_api_nowplaying(self, station_name):
        """Delegiert an radiode-Modul; verarbeitet Seiteneffekte (Logo, Sendername)."""
        artist, title, resolved_name, det_logo, search_logo = _radiode_get_nowplaying(
            self.api_client,
            self.plugin_slug,
            station_name,
            existing_logo=self.station_logo,
            debug_log=self._debug_log_api_raw
        )
        if resolved_name:
            self.set_property_safe(_P.STATION, resolved_name)
            self._record_verified_station_source(
                station_name=resolved_name,
                source_kind='radiode_api',
                confidence=0.95,
            )
        # Suche-Logo hat Vorrang (immer ueberschreiben), Details-Logo nur wenn noch keins vorhanden
        if search_logo:
            self.station_logo = search_logo
            self._ensure_radiode_identity_from_value(search_logo, context='radiode_search_logo')
            self.set_logo_safe()
        elif det_logo and not self.station_logo:
            self.station_logo = det_logo
            self._ensure_radiode_identity_from_value(det_logo, context='radiode_details_logo')
            self.set_logo_safe()
        return artist, title
    
    def api_metadata_worker(self, generation):
        """Fallback: Pollt verschiedene APIs wenn keine ICY-Metadaten verfügbar"""
        self._reconcile_api_source('api_metadata_worker_start')
        if not self._is_api_source_allowed():
            self._log_api_source_blocked('api_metadata_worker_start')
            return

        log_debug("API Metadata Worker gestartet (Fallback-Modus)")

        # Timer-Status beim Start des Workers sauber initialisieren.
        # Verhindert, dass ein alter Timer aus einem vorherigen Stream sofort greift.
        self._reset_song_timeout_state(clear_debug=True)

        last_title = ""
        poll_interval = API_METADATA_POLL_INTERVAL_S
        station_name = WINDOW.getProperty(_P.STATION)
        stream_url = self.current_url or ''

        try:
            while (
                not self.stop_thread
                and self.is_playing
                and self._is_api_source_allowed()
                and (self.use_api_fallback or self.plugin_slug or self.tunein_station_id)
                and generation == self.metadata_generation
            ):
                # station_name aktualisieren falls API in get_radiode_api_nowplaying
                # den korrekten Namen nachträglich gesetzt hat (z.B. via Details-API)
                fresh_station = WINDOW.getProperty(_P.STATION)
                if fresh_station:
                    station_name = fresh_station

                # Versuche verschiedene APIs (plugin_slug/tunein_station_id erlauben Abfrage ohne station_name)
                if station_name or self.plugin_slug or self.tunein_station_id:
                    artist, title = self.get_nowplaying_from_apis(station_name, stream_url)
                    
                    if title and title != last_title:
                        last_title = title
                        
                        # Setze Logo (nur wenn echtes Logo, sonst Kodi-Fallback)
                        self.set_logo_safe()
                        album, album_date, mbid, first_release, duration_ms = '', '', '', '', 0
                        display_artist = artist
                        display_title = title
                        if artist and title and self._is_pre_mb_song_pair((artist, title), station_name=station_name, source='api'):
                            mb_artist, mb_title, mb_album, mb_album_date, mbid, mb_first_release, uncertain, duration_ms = _identify_artist_title_via_musicbrainz(artist, title)
                            if uncertain:
                                mbid = ''
                                duration_ms = 0
                            else:
                                album = mb_album
                                album_date = mb_album_date
                                first_release = mb_first_release
                                a_sim = _mb_similarity(artist, mb_artist) if mb_artist else 0.0
                                t_sim = _mb_similarity(title, mb_title) if mb_title else 0.0
                                if self._is_dot_sensitive_artist_conflict(artist, mb_artist):
                                    log_info(
                                        f"MB-Bereinigung verworfen (API, Punkt-Konflikt): "
                                        f"'{artist}' vs '{mb_artist}'"
                                    )
                                    mbid = ''
                                    album = ''
                                    album_date = ''
                                    first_release = ''
                                    duration_ms = 0
                                elif mb_artist and mb_title and a_sim >= _MB_LABEL_CORRECTION_MIN_SIM and t_sim >= _MB_LABEL_CORRECTION_MIN_SIM:
                                    # MB bestaetigt denselben Song: korrigierte Schreibweise fuer Labels verwenden
                                    display_artist = mb_artist
                                    display_title = mb_title
                                    if display_artist != artist or display_title != title:
                                        log_info(
                                            f"MB-Bereinigung (API): '{artist} - {title}'"
                                            f" -> '{display_artist} - {display_title}'"
                                        )
                                elif mb_artist and mb_title and (a_sim < 0.8 or t_sim < 0.8):
                                    # MB hat anderen Song gefunden: MBID/Album verwerfen
                                    mbid = ''
                                    album = ''
                                    album_date = ''
                                    first_release = ''
                                    duration_ms = 0
                        elif artist and title:
                            log_info(
                                f"MB uebersprungen (pre_mb_policy, api): "
                                f"'{artist} - {title}'"
                            )

                        if display_artist:
                            # Reihenfolge: MBID und Title vor Artist setzen.
                            # AS lauscht auf RadioMonitor.Artist als Trigger und liest
                            # danach sofort RadioMonitor.ArtistMBID – daher muss MBID bereits
                            # gesetzt sein wenn Artist den Trigger auslöst.
                            self.set_property_safe(_P.TITLE, display_title)
                            self.set_property_safe(_P.STREAM_TTL, f"{display_artist} - {display_title}")
                            if album:
                                self.set_property_safe(_P.ALBUM, album)
                            else:
                                WINDOW.clearProperty(_P.ALBUM)
                            if album_date:
                                self.set_property_safe(_P.ALBUM_DATE, album_date)
                            else:
                                WINDOW.clearProperty(_P.ALBUM_DATE)
                            if mbid:
                                self.set_property_safe(_P.MBID, mbid)
                            else:
                                WINDOW.clearProperty(_P.MBID)
                            if first_release:
                                self.set_property_safe(_P.FIRST_REL, first_release)
                            else:
                                WINDOW.clearProperty(_P.FIRST_REL)
                            # Artist-Trigger zuerst setzen, dann Artist-Info nachziehen.
                            self.set_property_safe(_P.ARTIST, display_artist)
                            self.set_property_safe(_P.ARTIST_DISPLAY, display_artist)
                            log_info(f"API Update: {display_artist} - {display_title}")
                            if mbid and display_artist:
                                time.sleep(1)  # MusicBrainz Rate-Limit einhalten
                                band_formed, band_members, mb_genre = _musicbrainz_query_artist_info(mbid)
                                if band_formed:
                                    self.set_property_safe(_P.BAND_FORM, band_formed)
                                else:
                                    WINDOW.clearProperty(_P.BAND_FORM)
                                if band_members:
                                    self.set_property_safe(_P.BAND_MEM, band_members)
                                else:
                                    WINDOW.clearProperty(_P.BAND_MEM)
                                if mb_genre:
                                    self.set_property_safe(_P.GENRE, mb_genre)
                            else:
                                WINDOW.clearProperty(_P.BAND_FORM)
                                WINDOW.clearProperty(_P.BAND_MEM)

                            # Logo sofort nach Artist setzen
                            self.set_logo_safe()

                            # Push in laufende Kodi-Labels ist bei Streams nicht verlaesslich.
                            # Deshalb hier bewusst kein update_player_metadata().
                        else:
                            # Artist und MBID bewusst NICHT löschen —
                            # alten Wert behalten bis neuer gesichert ist.
                            # ArtistSlideshow zeigt weiter den letzten bekannten Künstler
                            # statt auf rohes ICY-Metadaten-Fallback zurückzufallen.
                            # Gelöscht wird nur beim echten Stream-Stop (clear_properties).
                            WINDOW.clearProperty(_P.ALBUM)
                            WINDOW.clearProperty(_P.ALBUM_DATE)
                            WINDOW.clearProperty(_P.FIRST_REL)
                            WINDOW.clearProperty(_P.BAND_FORM)
                            WINDOW.clearProperty(_P.BAND_MEM)
                            self.set_property_safe(_P.TITLE, title)
                            self.set_property_safe(_P.STREAM_TTL, title)
                            log_info(f"API Update: {title}")
                            
                            # Push in laufende Kodi-Labels ist bei Streams nicht verlaesslich.
                            # Deshalb hier bewusst kein update_player_metadata().

                        # Song-Timeout: Timer (neu) starten sobald ein Titel erkannt wurde.
                        self._start_song_timeout(
                            duration_ms,
                            song_key=(artist or '', title or ''),
                            station_name=station_name
                        )

                # Song-Timeout Anzeige aktualisieren und ggf. Properties loeschen.
                self._handle_song_timeout_expiry()

                # Warte vor nächster Abfrage
                for _ in range(poll_interval * 2):  # 10 Sekunden in 0.5s Schritten
                    if (
                        self.stop_thread
                        or not self.is_playing
                        or generation != self.metadata_generation
                    ):
                        break
                    time.sleep(0.5)
                
        except Exception as e:
            log_error(f"Fehler im API Metadata Worker: {str(e)}")
        finally:
            log_debug("API Metadata Worker beendet")

    def _musicplayer_metadata_fallback(self, generation):
        """Fallback für Streams ohne ICY und ohne radio.de-API.
        Pollt MusicPlayer.Artist/Title auf Änderungen (wie api_metadata_worker die radio.de API).
        Titelwechsel auf Live-Streams (z.B. Mother Earth Radio) werden so erkannt.
        Für Library-Streams (Ampache) beendet sich die Schleife beim nächsten onAVStarted
        automatisch via metadata_generation-Check.
        Wenn MusicPlayer leer bleibt, wird RadioMonitor.Playing gecleart.
        Station wird nicht gesetzt – es gibt keinen ICY-Stationsnamen.
        """
        log_debug("MusicPlayer-Fallback aktiv (kein ICY, kein API-Fallback)")

        last_artist = ''
        last_title = ''
        poll_interval = MUSICPLAYER_FALLBACK_POLL_INTERVAL_S

        try:
            while (
                not self.stop_thread
                and self.is_playing
                and generation == self.metadata_generation
            ):
                # MusicPlayer-Metadaten lesen
                try:
                    if not self.player.isPlayingAudio():
                        WINDOW.clearProperty(_P.PLAYING)
                        log_debug("MusicPlayer-Fallback: kein Audio - deaktiviere RadioMonitor")
                        return
                    info_tag = self.player.getMusicInfoTag()
                    mp_artist = (info_tag.getArtist() or '').strip()
                    mp_title = (info_tag.getTitle() or '').strip()
                except Exception as e:
                    log_debug(f"MusicPlayer-Fallback: Fehler beim Lesen der Metadaten: {e}")
                    WINDOW.clearProperty(_P.PLAYING)
                    return

                # Erster Durchlauf und beide leer → deaktivieren
                if not last_artist and not last_title and not mp_artist and not mp_title:
                    log_debug("MusicPlayer-Fallback: Artist und Title leer - deaktiviere RadioMonitor")
                    WINDOW.clearProperty(_P.PLAYING)
                    return

                # Titelwechsel (oder erster Durchlauf mit Inhalt)?
                if mp_artist != last_artist or mp_title != last_title:
                    last_artist = mp_artist
                    last_title = mp_title

                    if generation != self.metadata_generation:
                        return

                    log_info(f"MusicPlayer-Fallback: Artist='{mp_artist}', Title='{mp_title}'")

                    # MusicBrainz-Lookup
                    _, mb_artist, mb_title, mbid, mb_album, mb_album_date, mb_first_release, duration_ms = \
                        _musicbrainz_query_recording(mp_title, mp_artist)

                    # MB-Bereinigung: korrigierten Label nur verwenden wenn MB eindeutig
                    # denselben Song bestaetigt. Original fuer Aenderungs-Detektion behalten.
                    artist = mp_artist
                    title = mp_title
                    if self._is_dot_sensitive_artist_conflict(mp_artist, mb_artist):
                        log_info(
                            f"MB-Bereinigung verworfen (MP, Punkt-Konflikt): "
                            f"'{mp_artist}' vs '{mb_artist}'"
                        )
                        mbid = ''
                        mb_album = ''
                        mb_album_date = ''
                        mb_first_release = ''
                        duration_ms = 0
                    elif mb_artist and mb_title:
                        a_sim = _mb_similarity(mp_artist, mb_artist)
                        t_sim = _mb_similarity(mp_title, mb_title)
                        if a_sim >= _MB_LABEL_CORRECTION_MIN_SIM and t_sim >= _MB_LABEL_CORRECTION_MIN_SIM:
                            if mb_artist != mp_artist or mb_title != mp_title:
                                log_info(
                                    f"MB-Bereinigung (MP): '{mp_artist} - {mp_title}'"
                                    f" -> '{mb_artist} - {mb_title}'"
                                )
                            artist = mb_artist
                            title = mb_title

                    if generation != self.metadata_generation:
                        return

                    # Properties setzen – MBID vor Artist (AS-Trigger)
                    if title:
                        self.set_property_safe(_P.TITLE, title)
                    else:
                        WINDOW.clearProperty(_P.TITLE)
                    if mb_album:
                        self.set_property_safe(_P.ALBUM, mb_album)
                    else:
                        WINDOW.clearProperty(_P.ALBUM)
                    if mb_album_date:
                        self.set_property_safe(_P.ALBUM_DATE, mb_album_date)
                    else:
                        WINDOW.clearProperty(_P.ALBUM_DATE)
                    if mbid:
                        self.set_property_safe(_P.MBID, mbid)
                    else:
                        WINDOW.clearProperty(_P.MBID)
                    if mb_first_release:
                        self.set_property_safe(_P.FIRST_REL, mb_first_release)
                    else:
                        WINDOW.clearProperty(_P.FIRST_REL)
                    if artist:
                        self.set_property_safe(_P.ARTIST, artist)
                        self.set_property_safe(_P.ARTIST_DISPLAY, artist)
                        log_info(f"MusicPlayer-Fallback gesetzt: Artist='{artist}', Title='{title}', MBID='{mbid}'")
                    else:
                        WINDOW.clearProperty(_P.ARTIST)
                        WINDOW.clearProperty(_P.ARTIST_DISPLAY)
                        WINDOW.clearProperty(_P.PLAYING)
                        log_debug("MusicPlayer-Fallback: kein Artist ermittelbar - deaktiviere RadioMonitor")
                        return

                    # Player.Icon bei Titelwechsel neu lesen (z.B. AzuraCast liefert pro Song anderes Album-Cover)
                    try:
                        current_icon = xbmc.getInfoLabel('Player.Icon')
                        if current_icon and self.is_real_logo(current_icon):
                            self.station_logo = current_icon
                    except Exception:
                        pass
                    self.set_logo_safe()

                    if mbid and artist:
                        time.sleep(1)
                        band_formed, band_members, mb_genre = _musicbrainz_query_artist_info(mbid)
                        if band_formed:
                            self.set_property_safe(_P.BAND_FORM, band_formed)
                        else:
                            WINDOW.clearProperty(_P.BAND_FORM)
                        if band_members:
                            self.set_property_safe(_P.BAND_MEM, band_members)
                        else:
                            WINDOW.clearProperty(_P.BAND_MEM)
                        if mb_genre:
                            self.set_property_safe(_P.GENRE, mb_genre)
                    else:
                        WINDOW.clearProperty(_P.BAND_FORM)
                        WINDOW.clearProperty(_P.BAND_MEM)

                    # Song-Timeout: Timer (neu) starten sobald ein Titel erkannt wurde.
                    self._start_song_timeout(
                        duration_ms,
                        song_key=(artist or '', title or ''),
                        station_name=''
                    )

                # Song-Timeout Anzeige aktualisieren und ggf. Properties loeschen.
                self._handle_song_timeout_expiry()
                for _ in range(poll_interval * 2):
                    if (
                        self.stop_thread
                        or not self.is_playing
                        or generation != self.metadata_generation
                    ):
                        break
                    time.sleep(0.5)

        finally:
            log_debug("MusicPlayer-Fallback beendet")

    def parse_icy_metadata(self, url):
        """Liest ICY-Metadaten aus dem Stream"""
        try:
            self._capture_stream_url_raw(url)
            headers = {'Icy-MetaData': '1', **DEFAULT_HTTP_HEADERS}
            response = self.api_client.get(url, headers=headers, stream=True, timeout=5)
            self.raw_sources.set_json(_P.RAW_STREAM_HEADERS, dict(response.headers), max_len=20000)
            
            # KOMPLETT LOGGEN: Alle ICY-Header
            log_debug("=== ALLE ICY RESPONSE HEADERS ===")
            for header_name, header_value in response.headers.items():
                if 'icy' in header_name.lower() or 'ice' in header_name.lower():
                    log_debug(f"  {header_name}: {header_value}")
            log_debug("=================================")
            
            # ICY-Metadaten aus den Headers
            icy_name = response.headers.get('icy-name', '')
            icy_genre = response.headers.get('icy-genre', '')
            
            # Station initial aus ICY-Header icy-name (wird von API überschrieben falls verfügbar)
            station_name = icy_name
            if station_name:
                log_debug(f"Station (ICY): {station_name}")
                self._record_verified_station_source(
                    station_name=station_name,
                    stream_url=url,
                    source_kind='icy_header',
                    confidence=0.70,
                )
            else:
                log_debug("Kein icy-name im Header")
                hinted = self._apply_verified_source_hint(url)
                if hinted:
                    station_name = hinted
                    log_debug(f"Station aus verified source (URL): {station_name}")
            
            if icy_genre:
                log_debug(f"Genre: {icy_genre}")
            
            # Metaint - Position der Metadaten im Stream
            metaint = response.headers.get('icy-metaint')
            if not metaint:
                log_warning("Kein icy-metaint Header gefunden - Stream sendet keine ICY-Metadaten")
                self._setup_api_fallback_from_url(url)
                response.close()
                return None

            metaint = int(metaint)
            log_debug(f"MetaInt: {metaint}")
            
            return {'metaint': metaint, 'response': response, 'station': station_name, 'genre': icy_genre}
            
        except Exception as e:
            log_error(f"Fehler beim Abrufen der ICY-Metadaten: {str(e)}")
            self._setup_api_fallback_from_url(url)
            return None
            
    def extract_stream_title(self, metadata_raw):
        """Extrahiert den StreamTitle aus den rohen Metadaten (Nutzt zentrales metadata Modul)"""
        return _extract_stream_title(metadata_raw)
        
    def parse_stream_title(self, stream_title, station_name=None, stream_url=None):
        """
        Ermittelt den finalen Song-Kandidaten aus allen aktiven Quellen.
        Ablauf (vereinfacht):
        1. Kandidaten sammeln (ICY, API, optional MusicPlayer, optional ASM-QF).
        2. Source-Policies anwenden (Lock/Stale/Priorisierung).
        3. MusicBrainz bewertet Kandidaten und bestimmt den Gewinner.
        4. Bei MB=0 greifen definierte Source-Fallbacks (Lock/API/ICY).
        5. Letzter Fallback: bestehende ICY-Analyse.
        """
        # Parse-Zyklus starten, ohne die zuletzt gesetzte Quelle sofort zu loeschen.
        # Sonst flackert RadioMonitor.Source zwischen '' und dem finalen Gewinner.
        self._last_decision_source = ''
        self._last_decision_pair = ('', '')
        invalid = INVALID_METADATA_VALUES + ["", station_name]
        artist, title, is_von, has_multi = _parse_metadata_complex(stream_title, station_name)
        qf_authoritative = self._is_qf_authoritative()

        # --- Kandidaten sammeln ---
        candidates = []
        api_candidate = (None, None)
        api_changed = False
        mp_direct = (None, None)
        mp_swapped = (None, None)
        mp_pairs = []

        # MusicPlayer-Kandidaten optional lesen (aktiv via MP_DECISION_ENABLED
        # oder bei als verlaesslich erkanntem MP-Profil).
        if self._is_mp_decision_active() and not qf_authoritative:
            mp_direct, mp_swapped = self._read_musicplayer_candidates(invalid)
            mp_pairs = self._filter_non_generic_song_pairs(
                self._valid_song_pairs(mp_direct, mp_swapped),
                station_name
            )
            if mp_pairs and self._is_musicplayer_trusted():
                self.musicplayer_trust.reset_mismatch_if_trusted(self.metadata_generation)
            elif not mp_pairs and self._is_musicplayer_trusted():
                self._register_musicplayer_mismatch('MusicPlayer leer/ungueltig')

        # API-Kandidat
        api_candidate_available = bool(stream_url and (station_name or self.plugin_slug or self.tunein_station_id))
        if not qf_authoritative:
            if api_candidate_available and not self._is_api_source_allowed():
                self._log_api_source_blocked('parse_stream_title_api_first')
            if api_candidate_available and self._is_api_source_allowed():
                api_artist, api_title = self.get_nowplaying_from_apis(station_name, stream_url)
                api_artist, api_title = self._normalize_song_candidate(api_artist, api_title, invalid)
                if api_artist and api_title:
                    api_key = (api_artist, api_title)
                    if self._api_timeout_block_key and api_key == self._api_timeout_block_key:
                        log_info(f"API-Kandidat geblockt nach Timeout: '{api_artist} - {api_title}'")
                    else:
                        if self._api_timeout_block_key != ('', '') and api_key != self._api_timeout_block_key:
                            log_info(f"API-Song geaendert, Timeout-Block aufgehoben: '{api_artist} - {api_title}'")
                            self._api_timeout_block_key = ('', '')
                        if self._append_non_generic_candidate(
                            candidates,
                            'api',
                            api_artist,
                            api_title,
                            station_name
                        ):
                            if api_artist != api_title:
                                s_artist, s_title = self._normalize_song_candidate(api_title, api_artist, invalid)
                                self._append_non_generic_candidate(
                                    candidates,
                                    'api_swapped',
                                    s_artist,
                                    s_title,
                                    station_name
                                )
                            api_candidate = (api_artist, api_title)
                            api_changed = (self._last_seen_api_key != ('', '') and api_key != self._last_seen_api_key)
                            self._last_seen_api_key = api_key

        # ICY-Kandidaten (direkt + ggf. swapped)
        icy_artist, icy_title = (None, None)
        if not qf_authoritative:
            icy_artist, icy_title = self._normalize_song_candidate(artist, title, invalid)
            if self._append_non_generic_candidate(
                candidates,
                'icy',
                icy_artist,
                icy_title,
                station_name
            ):
                if not is_von and icy_artist != icy_title:
                    s_artist, s_title = self._normalize_song_candidate(icy_title, icy_artist, invalid)
                    self._append_non_generic_candidate(
                        candidates,
                        'icy_swapped',
                        s_artist,
                        s_title,
                        station_name
                    )

        icy_candidate_pairs = [
            (c.get('artist'), c.get('title'))
            for c in candidates
            if str(c.get('source', '')).startswith('icy')
        ]
        mp_candidates_allowed = False
        if self._is_mp_decision_active() and not qf_authoritative:
            mp_candidates_allowed = self._should_use_musicplayer_candidates(
                mp_pairs,
                api_candidate,
                icy_candidate_pairs,
                station_name
            )

        # MusicPlayer-Kandidaten ergänzen (direkt + swapped)
        if mp_candidates_allowed:
            if mp_direct[0] and mp_direct[1]:
                candidates.append({'source': 'musicplayer', 'artist': mp_direct[0], 'title': mp_direct[1]})
            if mp_swapped[0] and mp_swapped[1]:
                candidates.append({'source': 'musicplayer_swapped', 'artist': mp_swapped[0], 'title': mp_swapped[1]})

        # ASM-QF-Kandidaten ergänzen
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
                # Wenn ASM-QF valide Daten liefert, werden alle anderen
                # Kandidaten verworfen (Exklusiv-Modus).
                candidates[:] = [c for c in candidates if str(c.get('source', '')).startswith('asm-qf')]
            elif qf_authoritative:
                log_info("ASM-QF autoritativ: kein valider QF-Song -> kein Song")
                self._set_last_song_decision('', None, None)
                return None, None, '', '', '', '', 0

        trigger_reason = str(getattr(self, '_parse_trigger_reason', '') or '')
        candidates = self._apply_api_stale_override(
            candidates,
            trigger_reason,
            api_candidate,
            icy_candidate_pairs
        )

        locked_source = str(getattr(self, '_parse_locked_source', '') or '')
        candidates = self._apply_locked_source_policy(
            candidates,
            locked_source,
            api_candidate,
            icy_candidate_pairs,
            mp_pairs
        )

        stream_candidates, mp_only_candidates = self._split_source_candidates(candidates)
        prioritize_stream = self._should_prioritize_stream_candidates()
        profile_confidence = self._current_profile_confidence()
        log_debug(
            f"Quellen-Phase: stream_candidates={len(stream_candidates)}, "
            f"mp_candidates={len(mp_only_candidates)}, lock='{locked_source or 'none'}', "
            f"trigger='{trigger_reason or 'none'}', "
            f"mode='{'stream-priority' if prioritize_stream else 'neutral-all-sources'}', "
            f"profile_conf={profile_confidence:.2f}"
        )

        if prioritize_stream:
            primary_candidates = stream_candidates
            fallback_candidates = mp_only_candidates if not stream_candidates else []
        else:
            primary_candidates = list(stream_candidates) + list(mp_only_candidates)
            fallback_candidates = []

        winner, evaluations = self._select_mb_winner(primary_candidates)
        if winner:
            # Für source-locked Trigger immer das source-native Eingangspaar speichern.
            # MB-normalisierte Namen können sich vom API/ICY/MP-Original unterscheiden
            # und sonst fälschlich als "Quelle gewechselt" wirken.
            self._set_last_song_decision(
                winner.get('source'),
                winner.get('input_artist'),
                winner.get('input_title')
            )
            if self._is_mp_decision_active():
                self._log_musicplayer_comparison(
                    winner.get('source'),
                    (winner.get('input_artist'), winner.get('input_title')),
                    mp_pairs
                )
                self._update_musicplayer_trust_after_decision(
                    winner.get('source'),
                    (winner.get('input_artist'), winner.get('input_title')),
                    mp_pairs
                )
            return (
                winner['corrected_artist'],
                winner['corrected_title'],
                winner['mb_album'],
                winner['mb_album_date'],
                winner['mbid'],
                winner['mb_first_release'],
                winner['mb_duration_ms']
            )

        # Fallback nur im Stream-Prioritaetsmodus, wenn keine Stream-Kandidaten da waren.
        if fallback_candidates:
            fb_winner, fb_evaluations = self._select_mb_winner(fallback_candidates)
            if fb_winner:
                self._set_last_song_decision(
                    fb_winner.get('source'),
                    fb_winner.get('input_artist'),
                    fb_winner.get('input_title')
                )
                if self._is_mp_decision_active():
                    self._log_musicplayer_comparison(
                        fb_winner.get('source'),
                        (fb_winner.get('input_artist'), fb_winner.get('input_title')),
                        mp_pairs
                    )
                    self._update_musicplayer_trust_after_decision(
                        fb_winner.get('source'),
                        (fb_winner.get('input_artist'), fb_winner.get('input_title')),
                        mp_pairs
                    )
                return (
                    fb_winner['corrected_artist'],
                    fb_winner['corrected_title'],
                    fb_winner['mb_album'],
                    fb_winner['mb_album_date'],
                    fb_winner['mbid'],
                    fb_winner['mb_first_release'],
                    fb_winner['mb_duration_ms']
                )
            evaluations = fb_evaluations

        # Sonderfall B: MB kann zwischen Kandidaten nicht entscheiden (alle score=0).
        # Dann API nur übernehmen, wenn sie sich gegenüber der letzten API-Antwort geändert hat.
        # Sonst gilt: keine verlässlichen Songdaten -> Artist/Title leer lassen.
        if stream_candidates and evaluations and all(ev.get('score', 0) == 0 for ev in evaluations):
            # MB=0 für die Stream-Quelle: API/ICY intern entscheiden.
            # MusicPlayer wird danach nur zum Abgleich/Trust genutzt.
            icy_candidates = [
                (
                    str(ev.get('source', '') or ''),
                    (ev.get('input_artist'), ev.get('input_title'))
                )
                for ev in evaluations
                if (
                    str(ev.get('source', '')).startswith('icy')
                    and ev.get('input_artist')
                    and ev.get('input_title')
                )
            ]
            locked_source_family, locked_source_pair = self._resolve_mb_zero_with_source_lock(
                locked_source,
                mp_pairs,
                api_candidate,
                icy_candidates
            )
            if locked_source_family and locked_source_pair[0] and locked_source_pair[1]:
                if (
                    str(locked_source_family).startswith('icy')
                    and (
                        self._is_generic_song_pair(locked_source_pair, station_name)
                        or self._is_station_name_match_pair(locked_source_pair, station_name)
                        or self._is_obvious_non_song_text(
                            f"{locked_source_pair[0]} - {locked_source_pair[1]}"
                        )
                        or self._is_obvious_non_song_text(stream_title)
                    )
                ):
                    log_info(
                        "MB score=0, Source-Lock='icy' liefert generische/Nicht-Song-Daten -> "
                        "kein Song, nutze nur Station/StreamTitle"
                    )
                    self._set_last_song_decision('', None, None)
                    return None, None, '', '', '', '', 0
                log_info(
                    f"MB score=0 fuer alle Kandidaten, Source-Lock='{locked_source_family}' "
                    f"-> nutze gelockte Quelle: '{locked_source_pair[0]} - {locked_source_pair[1]}'"
                )
                self._set_last_song_decision(
                    locked_source_family,
                    locked_source_pair[0],
                    locked_source_pair[1]
                )
                if self._is_mp_decision_active():
                    self._log_musicplayer_comparison(
                        locked_source_family,
                        locked_source_pair,
                        mp_pairs
                    )
                    self._update_musicplayer_trust_after_decision(
                        locked_source_family,
                        locked_source_pair,
                        mp_pairs
                    )
                return locked_source_pair[0], locked_source_pair[1], '', '', '', '', 0

            has_icy_candidate = any(str(ev.get('source', '')).startswith('icy') for ev in evaluations)
            if api_candidate[0] and api_candidate[1] and (api_changed or not has_icy_candidate):
                reason = "API hat gewechselt" if api_changed else "kein valider ICY-Kandidat"
                log_info(
                    f"MB score=0 für alle Kandidaten, {reason} -> nutze API: "
                    f"'{api_candidate[0]} - {api_candidate[1]}'"
                )
                self._set_last_song_decision('api', api_candidate[0], api_candidate[1])
                if self._is_mp_decision_active():
                    self._log_musicplayer_comparison('api', api_candidate, mp_pairs)
                    self._update_musicplayer_trust_after_decision('api', api_candidate, mp_pairs)
                return api_candidate[0], api_candidate[1], '', '', '', '', 0
            # ICY-Rohdaten-Fallback: kein API, kein Lock, aber valider ICY-Split vorhanden.
            # MB kennt den Song nicht (z.B. DJ-Sets, Radiosendungen), der ICY-String ist
            # aber korrekt formatiert. Direkt-Paar (nicht swapped) nehmen.
            if has_icy_candidate:
                preferred_icy_source = (
                    'icy_swapped'
                    if (self._prefer_icy_swapped_from_history() or self._effective_icy_format_hint() == 'title_artist')
                    else 'icy'
                )
                icy_preferred = next(
                    (
                        (ev.get('input_artist'), ev.get('input_title'))
                        for ev in evaluations
                        if ev.get('source') == preferred_icy_source
                    ),
                    None
                )
                icy_fallback_pair = icy_preferred or next(
                    (
                        (ev.get('input_artist'), ev.get('input_title'))
                        for ev in evaluations
                        if str(ev.get('source', '')).startswith('icy')
                    ),
                    None
                )
                if icy_fallback_pair and icy_fallback_pair[0] and icy_fallback_pair[1]:
                    _a, _t = icy_fallback_pair
                    if (
                        self._is_generic_song_pair((_a, _t), station_name)
                        or self._is_station_name_match_pair((_a, _t), station_name)
                        or self._is_obvious_non_song_text(f"{_a} - {_t}")
                        or self._is_obvious_non_song_text(stream_title)
                    ):
                        log_info(
                            "MB score=0, ICY-Rohdaten sind generisch/Nicht-Song -> "
                            "kein Song, nutze nur Station/StreamTitle"
                        )
                        self._set_last_song_decision('', None, None)
                        return None, None, '', '', '', '', 0
                    # Defensiv: einzelne Zahl als Artist oder Title ist eine numerische Stream-ID,
                    # kein echter Song (z.B. "284684 - Real Title" oder "Artist - 399409").
                    _pure_num = re.compile(r'^\d+$')
                    if _pure_num.match(_a) or _pure_num.match(_t):
                        if api_candidate[0] and api_candidate[1]:
                            log_info(
                                f"MB score=0, ICY-Teil ist numerische ID "
                                f"('{_a} - {_t}') – Fallback auf API: "
                                f"'{api_candidate[0]} - {api_candidate[1]}'"
                            )
                            self._set_last_song_decision('api', api_candidate[0], api_candidate[1])
                            if self._is_mp_decision_active():
                                self._log_musicplayer_comparison('api', api_candidate, mp_pairs)
                                self._update_musicplayer_trust_after_decision('api', api_candidate, mp_pairs)
                            return api_candidate[0], api_candidate[1], '', '', '', '', 0
                        log_debug(
                            f"MB score=0, ICY-Teil ist numerische ID ('{_a} - {_t}'), kein API – kein Song"
                        )
                    else:
                        log_info(
                            f"MB score=0, kein API – ICY-Rohdaten-Fallback: "
                            f"'{_a} - {_t}'"
                        )
                        self._set_last_song_decision('icy', _a, _t)
                        if self._is_mp_decision_active():
                            self._log_musicplayer_comparison('icy', (_a, _t), mp_pairs)
                            self._update_musicplayer_trust_after_decision('icy', (_a, _t), mp_pairs)
                        return _a, _t, '', '', '', '', 0
            log_info(
                "MB score=0 für alle Kandidaten, keine belastbaren Songdaten -> "
                "nutze nur Station/StreamTitle")
            self._set_last_song_decision('', None, None)
            return None, None, '', '', '', '', 0

        # --- ICY-Analyse (bestehender Fallback) ---
        if (
            (artist and title and not self._is_pre_mb_song_pair((artist, title), station_name=station_name, source='icy'))
            or ((not artist) and title and self._is_generic_metadata_text(title, station_name))
        ):
            log_debug(f"ICY-Fallback verworfen (generisch): '{stream_title}'")
            self._set_last_song_decision('', None, None)
            return None, None, '', '', '', '', 0

        if not artist and not title:
            self._set_last_song_decision('', None, None)
            return None, None, '', '', '', '', 0

        # MusicBrainz zur Verifikation und Vervollständigung
        source_artist = artist
        source_title = title
        mb_first_release = ''
        duration_ms = 0
        
        if has_multi:
            # Mehrfaches ' - ' -> last-separator Variante prüfen
            alt_p1, alt_p2 = _get_last_separator_variant(stream_title)
            log_info(f"MusicBrainz: prüfe last-separator Variante: Title='{alt_p1}', Artist='{alt_p2}'")
            mb_artist, mb_title, mb_album, mb_album_date, mbid, mb_first_release, uncertain, duration_ms = _identify_artist_title_via_musicbrainz(alt_p1, alt_p2)
            if uncertain:
                # Fallback auf Standard-Split
                mb_artist, mb_title, mb_album, mb_album_date, mbid, mb_first_release, uncertain, duration_ms = _identify_artist_title_via_musicbrainz(artist, title)
        else:
            mb_artist, mb_title, mb_album, mb_album_date, mbid, mb_first_release, uncertain, duration_ms = _identify_artist_title_via_musicbrainz(artist, title)

        if uncertain:
            # Unsicherer MB-Treffer: nur Zusatzdaten verwerfen.
            mb_album, mb_album_date, mbid, mb_first_release, duration_ms = '', '', '', '', 0
        elif self._is_dot_sensitive_artist_conflict(source_artist, mb_artist):
            log_info(
                f"MB-Bereinigung verworfen (ICY, Punkt-Konflikt): "
                f"'{source_artist}' vs '{mb_artist}'"
            )
            mb_album, mb_album_date, mbid, mb_first_release, duration_ms = '', '', '', '', 0
            uncertain = True

        if source_artist in invalid:
            source_artist = None
        if source_title in invalid:
            source_title = None
        if not source_artist and not source_title:
            self._set_last_song_decision('', None, None)
            return None, None, '', '', '', '', 0

        # MB-Bereinigung: korrigierten Label nur verwenden wenn MB eindeutig denselben Song
        # bestaetigt. source_artist/title bleiben fuer internes Tracking unveraendert.
        display_artist = source_artist
        display_title = source_title
        if mb_artist and mb_title and not uncertain:
            a_sim = _mb_similarity(source_artist or '', mb_artist)
            t_sim = _mb_similarity(source_title or '', mb_title)
            if a_sim >= _MB_LABEL_CORRECTION_MIN_SIM and t_sim >= _MB_LABEL_CORRECTION_MIN_SIM:
                display_artist = mb_artist
                display_title = mb_title
                if display_artist != source_artist or display_title != source_title:
                    log_info(
                        f"MB-Bereinigung (ICY): '{source_artist} - {source_title}'"
                        f" -> '{display_artist} - {display_title}'"
                    )

        self._set_last_song_decision('icy', source_artist, source_title)
        if self._is_mp_decision_active():
            self._log_musicplayer_comparison('icy', (source_artist, source_title), mp_pairs)
            self._update_musicplayer_trust_after_decision('icy', (source_artist, source_title), mp_pairs)
        return display_artist, display_title, mb_album, mb_album_date, mbid, mb_first_release, duration_ms
        
    def metadata_worker(self, url, generation):
        """Worker-Thread zum kontinuierlichen Auslesen der Metadaten"""
        log_debug(f"Metadata Worker gestartet")

        # Timer-Status beim Start des Workers sauber initialisieren.
        # Gilt auch für den No-ICY-Pfad (API/MusicPlayer-Fallback).
        self._reset_song_timeout_state(clear_debug=True)
        self.startup_qualifier.reset_session()

        stream_info = self.parse_icy_metadata(url)
        if not stream_info:
            log_warning("Keine ICY-Metadaten verfuegbar - wechsle zu Fallback")
            WINDOW.clearProperty(_P.ICY_NOW)
            if not self._is_api_source_allowed():
                self._log_api_source_blocked('metadata_worker_no_icy')
            if (
                self._is_api_source_allowed()
                and (self.use_api_fallback or self.plugin_slug or self.tunein_station_id)
                and generation == self.metadata_generation
            ):
                self.api_metadata_worker(generation)
            elif generation == self.metadata_generation and self._is_mp_decision_active():
                self._musicplayer_metadata_fallback(generation)
            elif generation == self.metadata_generation:
                log_debug("Kein No-ICY-Fallback aktiv: MP-Entscheidung ist deaktiviert")
            return
            
        metaint = stream_info['metaint']
        response = stream_info.get('response')
        last_title = ""
        last_song_key = ('', '', '')
        last_winner_source = ''
        last_winner_pair = ('', '')
        initial_source_pending = False
        startup_stable_confirmed = False
        startup_qualify_until_ts = 0.0
        # Hinweis: response.raw.read() blockiert bis Daten da sind; bei Netzabbruch
        # kann das erst enden, wenn der Thread per stop_thread gestoppt wird.
        try:
            while (
                not self.stop_thread
                and self.is_playing
                and generation == self.metadata_generation
            ):
                try:
                    audio_data = response.raw.read(metaint)
                    if not audio_data:
                        break
                        
                    # Metadaten-Länge lesen (1 Byte * 16)
                    meta_length_byte = response.raw.read(1)
                    if not meta_length_byte:
                        break
                        
                    meta_length = ord(meta_length_byte) * 16
                    
                    if (
                        meta_length > 0
                        or last_winner_source.startswith('musicplayer')
                        or last_winner_source.startswith('api')
                        or initial_source_pending
                    ):
                        if meta_length > 0:
                            # Metadaten lesen
                            metadata = response.raw.read(meta_length)
                            if generation != self.metadata_generation:
                                break
                            metadata_str = metadata.decode('utf-8', errors='ignore').strip('\x00')

                            # KOMPLETT LOGGEN: Rohe ICY-Metadaten
                            if metadata_str:
                                log_debug("=== ICY METADATA (ROH) ===")
                                log_debug(metadata_str)
                                log_debug("=========================")
                                self.raw_sources.set_text(_P.RAW_ICY_METADATA, metadata_str, max_len=12000)

                            stream_title = self.extract_stream_title(metadata_str)
                        else:
                            # Bei aktivem MusicPlayer/API-Lock auch ohne neuen ICY-Block
                            # Quellenwechsel pruefen (z.B. wenn Sender selten ICY schreibt).
                            stream_title = last_title

                        station_name = stream_info.get('station', '') or (WINDOW.getProperty(_P.STATION) or '')
                        station_for_policy = station_name
                        invalid_values = INVALID_METADATA_VALUES + ["", station_name]
                        current_mp_pair = ('', '')
                        if self._is_mp_decision_active():
                            mp_direct_live, mp_swapped_live = self._read_musicplayer_candidates(invalid_values)
                            mp_live_pairs = self._valid_song_pairs(mp_direct_live, mp_swapped_live)
                            current_mp_pair = self._select_musicplayer_pair_for_source(
                                last_winner_source,
                                mp_live_pairs,
                                last_winner_pair=last_winner_pair
                            )
                            current_mp_pair = self._sanitize_musicplayer_pair(current_mp_pair, station_name)
                        icy_artist, icy_title = self.parse_stream_title_simple(stream_title or '')
                        icy_artist, icy_title = self._normalize_song_candidate(
                            icy_artist,
                            icy_title,
                            invalid_values
                        )
                        current_icy_pair = self._pair_for_source(
                            last_winner_source,
                            (icy_artist, icy_title)
                        )
                        current_icy_pair = self._sanitize_stream_source_pair(current_icy_pair, station_name)

                        # API-Daten erst nach stabilem Start oder nach gesetzter Erstquelle aktualisieren.
                        # Dadurch wird waehrend sichtbarem Kodi-Buffering kein API-Property vorbefuellt.
                        api_refresh_allowed = startup_stable_confirmed or bool(last_winner_source)
                        if api_refresh_allowed:
                            self._refresh_api_nowplaying_property(station_name)
                            current_api_pair = self._pair_for_source(last_winner_source, self._latest_api_pair)
                            current_api_pair = self._sanitize_stream_source_pair(current_api_pair, station_name)
                        else:
                            self._latest_api_pair = ('', '')
                            current_api_pair = ('', '')

                        # ASM-QF-Daten synchronisieren und auslesen
                        current_qf_pair = ('', '')
                        qf_authoritative = False
                        if self._qf_enabled:
                            self._sync_qf_result_property()
                            qf_require_fresh = not str(last_winner_source or '').startswith('asm-qf')
                            current_qf_pair = self._current_qf_hit_pair(
                                invalid_values,
                                require_fresh=qf_require_fresh
                            )
                            current_qf_pair = self._sanitize_stream_source_pair(current_qf_pair, station_name)
                            qf_authoritative = self._is_qf_authoritative()

                        # QF-Dominanz: Solange QF autoritativ ist, werden andere Quellen in diesem
                        # Durchlauf nicht fuer die Songentscheidung genutzt.
                        qf_exclusive = bool(self._qf_enabled and qf_authoritative)
                        if qf_exclusive:
                            current_mp_pair = ('', '')
                            current_icy_pair = ('', '')
                            current_api_pair = ('', '')
                            # Lokale Variablen für die Trigger-Prüfung ebenfalls leeren,
                            # damit keine Misch-Entscheidungen entstehen.
                            icy_artist, icy_title = ('', '')
                            # API-Label ebenfalls leeren, damit im Exklusiv-Modus keine
                            # alten API-Reste in der UI stehen.
                            WINDOW.clearProperty(_P.API_NOW)
                        elif self._qf_enabled:
                            # QF ist aktiv, aber im Ausnahmefall (Fehler/Timeout) darf auf
                            # API/ICY/MP zurückgefallen werden.
                            pass

                        # StreamTitle unabhängig vom Gewinner aktuell halten.
                        stream_title_changed = (stream_title != last_title)
                        if stream_title_changed:
                            last_title = stream_title
                            log_debug(f"Neuer StreamTitle erkannt: '{stream_title}'")
                            self.raw_sources.set_text(_P.RAW_ICY_STREAMTITLE, stream_title, max_len=4000)
                            self._last_icy_format_hint = self._classify_icy_format(stream_title, station_name)
                            self.raw_sources.set_json(
                                _P.RAW_ICY_PARSED,
                                {
                                    'artist': icy_artist or '',
                                    'title': icy_title or '',
                                    'format_hint': self._last_icy_format_hint or 'unknown'
                                },
                                max_len=12000
                            )

                        self.startup_qualifier.update_session_characteristics(
                            current_api_pair,
                            current_icy_pair,
                            station_name
                        )

                        needs_initial_decision = (
                            not last_winner_source
                            and (stream_title_changed or initial_source_pending)
                        )
                        if needs_initial_decision:
                            if stream_title_changed:
                                initial_source_pending = True

                            if not startup_stable_confirmed:
                                if not self._wait_for_stable_playback_start(generation):
                                    continue
                                startup_stable_confirmed = True
                                startup_qualify_until_ts = time.time() + STARTUP_SOURCE_QUALIFY_WINDOW_S
                                # Nach dem Wait im selben Durchlauf die Initial-Entscheidung erzwingen.
                                initial_source_pending = True
                                # MP/API nach dem Wait neu lesen, damit die Quellenwahl
                                # den tatsaechlich stabilen Startzustand verwendet.
                                if self._is_mp_decision_active():
                                    mp_direct_live, mp_swapped_live = self._read_musicplayer_candidates(invalid_values)
                                    mp_live_pairs = self._valid_song_pairs(mp_direct_live, mp_swapped_live)
                                    current_mp_pair = self._select_musicplayer_pair_for_source(
                                        last_winner_source,
                                        mp_live_pairs,
                                        last_winner_pair=last_winner_pair
                                    )
                                    current_mp_pair = self._sanitize_musicplayer_pair(current_mp_pair, station_name)
                                else:
                                    current_mp_pair = ('', '')
                                icy_artist, icy_title = self.parse_stream_title_simple(stream_title or '')
                                icy_artist, icy_title = self._normalize_song_candidate(
                                    icy_artist,
                                    icy_title,
                                    invalid_values
                                )
                                current_icy_pair = self._pair_for_source(
                                    last_winner_source,
                                    (icy_artist, icy_title)
                                )
                                current_icy_pair = self._sanitize_stream_source_pair(current_icy_pair, station_name)
                                self._refresh_api_nowplaying_property(station_name)
                                current_api_pair = self._pair_for_source(last_winner_source, self._latest_api_pair)
                                current_api_pair = self._sanitize_stream_source_pair(current_api_pair, station_name)
                                if station_name:
                                    self.set_property_safe(_P.STATION, station_name)
                                if stream_info.get('genre'):
                                    self.set_property_safe(_P.GENRE, stream_info.get('genre'))

                        self._try_enable_station_profile_policy(
                            station_name,
                            startup_stable_confirmed,
                            current_icy_pair,
                            current_api_pair
                        )

                        in_startup_window = (
                            not last_winner_source
                            and startup_stable_confirmed
                            and startup_qualify_until_ts > 0.0
                            and time.time() < startup_qualify_until_ts
                        )
                        startup_consensus = self.startup_qualifier.has_startup_source_consensus(
                            current_mp_pair,
                            current_api_pair,
                            current_icy_pair,
                            station_name
                        )
                        allow_initial_trigger = (not in_startup_window) or startup_consensus

                        # Source-locked Trigger: nur die letzte Gewinnerquelle entscheidet den Wechsel-Trigger.
                        stream_title_changed_for_policy = stream_title_changed
                        if str(last_winner_source or '').startswith('asm-qf'):
                            # Bei ASM-QF-Lock darf ICY-StreamTitle keinen Trigger beeinflussen.
                            stream_title_changed_for_policy = False

                        source_changed_trigger, trigger_reason = self._determine_source_change_trigger(
                            last_winner_source,
                            last_winner_pair,
                            current_mp_pair,
                            current_api_pair,
                            current_icy_pair,
                            station_name,
                            stream_title_changed_for_policy,
                            (initial_source_pending if allow_initial_trigger else False),
                            current_qf_pair=current_qf_pair
                        )

                        mp_generic_hold_active = False
                        if self._is_mp_decision_active():
                            mp_generic_hold_active = self._update_mp_generic_hold_state(
                                last_winner_source,
                                current_mp_pair,
                                station_name
                            )
                        if mp_generic_hold_active and source_changed_trigger:
                            log_debug(
                                f"MP-Generic-Hold: Trigger geparkt "
                                f"(reason='{trigger_reason}', source bleibt='musicplayer')"
                            )
                            source_changed_trigger = False
                            initial_source_pending = False

                        initial_program_block = (
                            not last_winner_source
                            and (
                                self._is_generic_song_pair(current_icy_pair, station_name)
                                or self._is_generic_stream_title(stream_title, station_name)
                            )
                            and not self._has_non_generic_song_pair(current_mp_pair, station_name)
                        )
                        if initial_program_block and source_changed_trigger:
                            if self.startup_qualifier.should_bypass_initial_program_block(
                                station_name,
                                current_mp_pair,
                                current_api_pair,
                                current_icy_pair
                            ):
                                log_info(
                                    f"Initialer Song-Block aufgehoben: "
                                    f"API-only-Verhalten erkannt (profil/heuristik)"
                                )
                                initial_program_block = False
                        if initial_program_block and source_changed_trigger:
                            block_detail = "ICY/MP noch generisch (Nachrichten/Programmphase)"
                            if not self._is_mp_decision_active():
                                block_detail = "ICY noch generisch (Nachrichten/Programmphase)"
                            log_info(
                                f"Initialer Song-Trigger unterdrueckt: "
                                f"{block_detail}"
                            )
                            source_changed_trigger = False
                            initial_source_pending = False
                        elif in_startup_window and not startup_consensus and source_changed_trigger:
                            remaining = max(0, int(round(startup_qualify_until_ts - time.time())))
                            log_debug(
                                f"Startup-Qualify aktiv: Trigger geparkt "
                                f"(noch {remaining}s, warte auf Quell-Konsens)"
                            )
                            source_changed_trigger = False
                            initial_source_pending = True

                        if source_changed_trigger:
                            observed_source = last_winner_source or 'initial'
                            log_debug(
                                f"Songwechsel-Trigger aktiv: reason='{trigger_reason}', "
                                f"beobachtete_Quelle='{observed_source}'"
                            )
                            # Bei Trigger: MusicBrainz-Cache invalidieren
                            try:
                                _mb_cache.clear()
                                log_debug(f"MB-Cache invalidiert wegen {trigger_reason}")
                            except Exception:
                                pass
                            if self._is_mp_decision_active() and trigger_reason.startswith('MusicPlayer'):
                                log_debug(
                                    f"MusicPlayer-Titelwechsel erkannt (trusted): "
                                    f"'{current_mp_pair[0]} - {current_mp_pair[1]}'"
                                )
                            stream_title = self._resolve_stream_title_for_trigger(
                                trigger_reason,
                                stream_title,
                                current_mp_pair
                            )

                            # Station stammt initial aus ICY-Header, kann spaeter von API validiert werden.
                            log_info(f"ICY-Daten: station='{station_name}', stream_title='{stream_title}'")

                            # Artist und Title trennen – API wird intern in parse_stream_title aufgerufen
                            parse_locked_source = last_winner_source
                            if self._is_mp_decision_active() and trigger_reason == self.TRIGGER_MP_CHANGE:
                                parse_locked_source = 'musicplayer'
                            elif trigger_reason == self.TRIGGER_ICY_STALE:
                                parse_locked_source = 'icy'
                            elif (
                                self._has_station_analysis()
                                and self._policy_preferred_source in ('musicplayer', 'api', 'icy')
                            ):
                                parse_locked_source = self._policy_preferred_source
                            self._parse_prev_winner_pair = last_winner_pair
                            self._parse_trigger_reason = trigger_reason
                            self._parse_locked_source = parse_locked_source
                            try:
                                artist, title, album, album_date, mbid, first_release, duration_ms = self.parse_stream_title(stream_title, station_name, url)
                            finally:
                                self._parse_prev_winner_pair = ('', '')
                                self._parse_trigger_reason = ''
                                self._parse_locked_source = ''
                            decision_source = self._last_decision_source
                            decision_pair = self._last_decision_pair
                            if self._is_mp_decision_active():
                                decision_source, decision_pair = self._maybe_reclaim_musicplayer_source(
                                    decision_source,
                                    decision_pair,
                                    current_mp_pair,
                                    station_name
                                )
                            current_song_key = (artist or '', title or '', mbid or '')
                            is_new_song = (current_song_key != last_song_key)

                            # Wenn beide None sind (z.B. bei Zahlen-IDs ohne API-Daten), überspringe diesen Titel
                            if artist is None and title is None:
                                log_debug(f"Keine verwertbaren Metadaten fuer '{stream_title}' - RadioMonitor Properties bleiben leer")
                                # Bei klar fehlenden Songdaten: nur Song-Properties löschen,
                                # Station + StreamTitle bleiben gesetzt.
                                if stream_title and stream_title not in INVALID_METADATA_VALUES:
                                    self.set_property_safe(_P.STREAM_TTL, stream_title)
                                    self._set_icy_nowplaying_label(stream_title=stream_title)
                                else:
                                    WINDOW.clearProperty(_P.STREAM_TTL)
                                    WINDOW.clearProperty(_P.ICY_NOW)
                                WINDOW.clearProperty(_P.ARTIST)
                                WINDOW.clearProperty(_P.ARTIST_DISPLAY)
                                WINDOW.clearProperty(_P.TITLE)
                                WINDOW.clearProperty(_P.ALBUM)
                                WINDOW.clearProperty(_P.ALBUM_DATE)
                                WINDOW.clearProperty(_P.MBID)
                                WINDOW.clearProperty(_P.FIRST_REL)
                                WINDOW.clearProperty(_P.BAND_FORM)
                                WINDOW.clearProperty(_P.BAND_MEM)
                                WINDOW.clearProperty(_P.GENRE)
                                self._reset_song_timeout_state(clear_debug=True)  # kein gültiger Song → Timer deaktivieren
                                self._emit_analysis_event(
                                    station_name=station_name,
                                    stream_title=stream_title,
                                    trigger_reason=trigger_reason,
                                    decision_source='',
                                    decision_pair=('', ''),
                                    current_api_pair=current_api_pair,
                                    current_icy_pair=current_icy_pair,
                                    current_mp_pair=current_mp_pair,
                                    source_changed=source_changed_trigger,
                                    note='no_usable_metadata'
                                )
                                last_winner_source = ''
                                last_winner_pair = ('', '')
                                # Kein verwertbares Ergebnis: initial_source_pending zuruecksetzen,
                                # damit die Policy nicht sofort wieder triggert (Busy-Loop).
                                # Bei naechstem echten StreamTitle-Wechsel wird es erneut gesetzt.
                                initial_source_pending = False
                                continue
                            
                            if stream_title not in INVALID_METADATA_VALUES:
                                self.set_property_safe(_P.STREAM_TTL, stream_title)
                                self._set_icy_nowplaying_label(stream_title=stream_title)
                            
                            # Reihenfolge: Title und MBID vor Artist setzen.
                            # AS lauscht auf RadioMonitor.Artist als Trigger und liest
                            # danach sofort RadioMonitor.ArtistMBID – daher muss MBID bereits
                            # gesetzt sein wenn Artist den Trigger auslöst.
                            if title:
                                self.set_property_safe(_P.TITLE, title)
                                log_debug(f"Title: {title}")
                            else:
                                WINDOW.clearProperty(_P.TITLE)
                                title = ''
                            if album:
                                self.set_property_safe(_P.ALBUM, album)
                                log_debug(f"Album: {album}")
                            else:
                                WINDOW.clearProperty(_P.ALBUM)
                            if album_date:
                                self.set_property_safe(_P.ALBUM_DATE, album_date)
                                log_debug(f"AlbumDate: {album_date}")
                            else:
                                WINDOW.clearProperty(_P.ALBUM_DATE)
                            if mbid:
                                self.set_property_safe(_P.MBID, mbid)
                                log_debug(f"MBID: {mbid}")
                            else:
                                WINDOW.clearProperty(_P.MBID)
                            if first_release:
                                self.set_property_safe(_P.FIRST_REL, first_release)
                                log_debug(f"FirstRelease: {first_release}")
                            else:
                                WINDOW.clearProperty(_P.FIRST_REL)
                            if artist:
                                self.set_property_safe(_P.ARTIST, artist)
                                self.set_property_safe(_P.ARTIST_DISPLAY, artist)
                                log_debug(f"Artist: {artist}")
                            else:
                                WINDOW.clearProperty(_P.ARTIST)
                                WINDOW.clearProperty(_P.ARTIST_DISPLAY)
                                artist = ''

                            # Logo sofort nach Artist setzen – vor dem optionalen Artist-Info-Call,
                            # damit der time.sleep(1) das Logo nicht verzögert.
                            self.set_logo_safe()

                            # Song-Timeout: Timer (neu) starten sobald ein Titel erkannt wurde.
                            # Bei MB-Laenge: Laenge - SONG_TIMEOUT_EARLY_CLEAR_S.
                            # Ohne MB-Laenge greift SONG_TIMEOUT_FALLBACK_S.
                            if title and is_new_song:
                                self._start_song_timeout(
                                    duration_ms,
                                    song_key=(artist or '', title or ''),
                                    station_name=station_name
                                )

                            # Artist-Info (Gründungsjahr + Mitglieder) erst NACH Artist-Property setzen.
                            # Grund: RadioMonitor.Artist ist der AS-Trigger – er darf nicht durch den
                            # sleep im Artist-Lookup verzögert werden. Die MBID ist nur gesetzt wenn
                            # MB den Artist sicher bestimmt hat (uncertain=False), daher ist sie hier
                            # ein verlässlicher Indikator für einen validen Artist-Match.
                            if mbid and artist:
                                time.sleep(1)  # MusicBrainz Rate-Limit einhalten
                                band_formed, band_members, mb_genre = _musicbrainz_query_artist_info(mbid)
                                if band_formed:
                                    self.set_property_safe(_P.BAND_FORM, band_formed)
                                    log_debug(f"BandFormed: {band_formed}")
                                else:
                                    WINDOW.clearProperty(_P.BAND_FORM)
                                if band_members:
                                    self.set_property_safe(_P.BAND_MEM, band_members)
                                    log_debug(f"BandMembers: {band_members}")
                                else:
                                    WINDOW.clearProperty(_P.BAND_MEM)
                                if mb_genre:
                                    self.set_property_safe(_P.GENRE, mb_genre)
                                    log_debug(f"Genre (MB): {mb_genre}")
                            else:
                                WINDOW.clearProperty(_P.BAND_FORM)
                                WINDOW.clearProperty(_P.BAND_MEM)
                            
                            # DEBUG: Zeige alle gesetzten Properties
                            log_debug("=== PROPERTIES GESETZT ===")
                            log_debug(f"RadioMonitor.Playing = {WINDOW.getProperty(_P.PLAYING)}")
                            log_debug(f"RadioMonitor.Station = {WINDOW.getProperty(_P.STATION)}")
                            log_debug(f"RadioMonitor.Artist = {WINDOW.getProperty(_P.ARTIST)}")
                            log_debug(f"RadioMonitor.Title = {WINDOW.getProperty(_P.TITLE)}")
                            log_debug(f"RadioMonitor.Album = {WINDOW.getProperty(_P.ALBUM)}")
                            log_debug(f"RadioMonitor.AlbumDate = {WINDOW.getProperty(_P.ALBUM_DATE)}")
                            log_debug(f"RadioMonitor.ArtistMBID = {WINDOW.getProperty(_P.MBID)}")
                            log_debug(f"RadioMonitor.FirstRelease = {WINDOW.getProperty(_P.FIRST_REL)}")
                            log_debug(f"RadioMonitor.BandFormed = {WINDOW.getProperty(_P.BAND_FORM)}")
                            log_debug(f"RadioMonitor.BandMembers = {WINDOW.getProperty(_P.BAND_MEM)}")
                            log_debug(f"RadioMonitor.StreamTitle = {WINDOW.getProperty(_P.STREAM_TTL)}")
                            log_debug(f"RadioMonitor.Genre = {WINDOW.getProperty(_P.GENRE)}")
                            log_debug(f"RadioMonitor.Logo = {WINDOW.getProperty(_P.LOGO)}")

                            # Push in laufende Kodi-Labels ist bei Streams nicht verlaesslich.
                            # Deshalb kein update_player_metadata()/JSON-RPC-Overwrite.
                            
                            self._capture_playing_item_raw()
                            self._capture_jsonrpc_player_raw()
                            
                            log_debug("========================")
                            
                            log_info(
                                f"Neuer Titel: {title if title else stream_title} "
                                f"(Artist: {artist if artist else 'N/A'}, "
                                f"Title: {title if title else 'N/A'}, "
                                f"Album: {album if album else 'N/A'}, "
                                f"StreamTitleRaw: {stream_title if stream_title else 'N/A'})"
                            )
                            if title:
                                last_song_key = current_song_key
                                self._persist_confirmed_song_if_allowed(
                                    station_for_policy,
                                    artist,
                                    title,
                                    mbid
                                )
                            if decision_source:
                                last_winner_source = decision_source
                                initial_source_pending = False
                                if decision_pair[0] and decision_pair[1]:
                                    last_winner_pair = decision_pair
                                else:
                                    last_winner_pair = (artist or '', title or '')
                            self._emit_analysis_event(
                                station_name=station_name,
                                stream_title=stream_title,
                                trigger_reason=trigger_reason,
                                decision_source=decision_source or '',
                                decision_pair=decision_pair or ('', ''),
                                current_api_pair=current_api_pair,
                                current_icy_pair=current_icy_pair,
                                current_mp_pair=current_mp_pair,
                                source_changed=source_changed_trigger,
                                note='title_applied'
                            )
                        elif (
                            stream_title_changed
                            and last_winner_source
                            and not str(last_winner_source).startswith('asm-qf')
                        ):
                            log_debug(
                                f"StreamTitle-Wechsel ignoriert: beobachtete_Quelle='{last_winner_source}' "
                                f"hat keinen Wechsel gemeldet"
                            )
                            self._emit_analysis_event(
                                station_name=station_name,
                                stream_title=stream_title,
                                trigger_reason='none',
                                decision_source=last_winner_source,
                                decision_pair=last_winner_pair,
                                current_api_pair=current_api_pair,
                                current_icy_pair=current_icy_pair,
                                current_mp_pair=current_mp_pair,
                                source_changed=False,
                                note='streamtitle_ignored_no_source_change'
                            )

                    # Song-Timeout Anzeige aktualisieren und ggf. Properties loeschen.
                    if startup_stable_confirmed or last_winner_source:
                        self._refresh_api_nowplaying_property(station_for_policy)
                    self._update_station_profile(station_for_policy)

                    # Generic-Keywords erfassen: nur wenn kein Song aktiv/bestätigt
                    if station_for_policy:
                        if not (last_winner_pair and last_winner_pair[0] and last_winner_pair[1]) \
                                and not (last_song_key and last_song_key[0]):
                            kw_texts = [
                                stream_title or self._label_from_pair(current_icy_pair) or '',
                                self._label_from_pair(current_api_pair) or '',
                            ]
                            self._collect_keyword_observations(station_for_policy, kw_texts)

                    if (
                        self.song_end_detector_enabled
                        and last_song_key[0]
                        and last_song_key[1]
                        and self._last_song_time
                    ):
                        aux_pairs = self._get_aux_source_pairs_for_song_end(station_for_policy)
                        source_pairs = {
                            'api': current_api_pair,
                            'icy': current_icy_pair,
                            'listitem': aux_pairs.get('listitem', ('', '')),
                            'playing_item': aux_pairs.get('playing_item', ('', '')),
                            'jsonrpc': aux_pairs.get('jsonrpc', ('', '')),
                        }
                        source_texts = {
                            'api': self._label_from_pair(current_api_pair) or (WINDOW.getProperty(_P.API_NOW) or ''),
                            'icy': stream_title or self._label_from_pair(current_icy_pair),
                            'listitem': self._label_from_pair(aux_pairs.get('listitem')),
                            'playing_item': self._label_from_pair(aux_pairs.get('playing_item')),
                            'jsonrpc': self._label_from_pair(aux_pairs.get('jsonrpc')),
                        }
                        end_policy = self._get_station_song_end_policy(station_for_policy)
                        detector_result = self.song_end_detector.evaluate(
                            now_ts=time.time(),
                            station_name=station_for_policy,
                            last_song_key=last_song_key,
                            song_started_ts=self._last_song_time,
                            song_timeout_s=self._song_timeout,
                            source_pairs=source_pairs,
                            source_texts=source_texts,
                            policy=end_policy,
                        )
                        candidate_keywords = detector_result.get('candidate_keywords') or []
                        matched_keywords = detector_result.get('matched_keywords') or []
                        keyword_candidates = list(dict.fromkeys(list(candidate_keywords) + list(matched_keywords)))
                        if keyword_candidates:
                            self._record_station_keyword_stats(station_for_policy, keyword_candidates)
                        if detector_result.get('should_clear'):
                            detector_reason = detector_result.get('reason') or 'mehrere Evidenzen'
                            log_info(
                                f"Songende-Detektor geloest aus: reason='{detector_reason}', "
                                f"matched={matched_keywords}"
                            )
                            self._clear_song_properties(
                                reason_text=(
                                    f"Songende-Detektor: loesche Song-Properties "
                                    f"(reason={detector_reason})"
                                ),
                                last_song_key=last_song_key,
                                enable_api_block=True
                            )
                            self._emit_analysis_event(
                                station_name=station_for_policy,
                                stream_title=stream_title,
                                trigger_reason='song_end_detector',
                                decision_source=last_winner_source,
                                decision_pair=last_winner_pair,
                                current_api_pair=current_api_pair,
                                current_icy_pair=current_icy_pair,
                                current_mp_pair=current_mp_pair,
                                source_changed=False,
                                note=f"song_end_detector_clear:{detector_reason}"
                            )
                    # Laeuft jede Iteration (~1s) - kein extra Thread notwendig.
                    self._handle_song_timeout_expiry(
                        last_song_key=last_song_key,
                        enable_api_block=True
                    )  # Verhindert wiederholtes Loeschen

                except Exception as e:
                    log_error(f"Fehler im Metadata-Loop (Thread laeuft weiter): {str(e)}")
                    time.sleep(1)
                    continue

        except Exception as e:
            log_error(f"Fehler im Metadata Worker: {str(e)}")
        finally:
            try:
                if response is not None:
                    response.close()
            except Exception as e:
                log_debug(f"Stream-Response konnte nicht geschlossen werden: {e}")
            log_debug("Metadata Worker beendet")
            
    def start_metadata_monitoring(self, url):
        """Startet das Metadata-Monitoring in einem separaten Thread"""
        self.stop_metadata_monitoring()
        
        # Reset flags
        self.use_api_fallback = False
        self.stop_thread = False
        self.metadata_generation += 1
        self._reset_musicplayer_trust_state('neuer metadata worker')
        generation = self.metadata_generation
        
        self.metadata_thread = threading.Thread(target=self.metadata_worker, args=(url, generation))
        self.metadata_thread.daemon = True
        self.metadata_thread.start()

    def stop_metadata_monitoring(self):
        """Stoppt das Metadata-Monitoring"""
        if self.metadata_thread and self.metadata_thread.is_alive():
            self.stop_thread = True
            self.metadata_generation += 1
            self.metadata_thread.join(timeout=0.5)  # kurz warten, Thread bricht selbst ab da is_playing=False
            if not self.metadata_thread.is_alive():
                self.metadata_thread = None
            
    def check_playing(self):
        """Überprüft, was gerade abgespielt wird"""
        if self.player.isPlaying():
            try:
                # Nur Audio-Streams überwachen, kein Video
                if not self.player.isPlayingAudio():
                    if self.is_playing:
                        self.is_playing = False
                        self.current_url = None
                        self.stop_metadata_monitoring()
                        self.clear_properties()
                        log_debug(f"Video läuft - kein Radio-Monitoring")
                    return

                # URL des aktuellen Streams
                playing_file = self.player.getPlayingFile()
                
                # Prüfen ob es ein Stream ist (http/https)
                if playing_file.startswith('http://') or playing_file.startswith('https://'):
                    
                    if playing_file != self.current_url:
                        if self.current_url:
                            self._handle_stream_transition(
                                f"check_playing: URL-Wechsel erkannt ({self.current_url} -> {playing_file})"
                            )
                        self.current_url = playing_file
                        self.is_playing = True
                        self._capture_stream_url_raw(playing_file)
                        self._capture_listitem_raw('check_playing_new_url')
                        self._capture_playing_item_raw()
                        self._capture_jsonrpc_player_raw()
                        self._ensure_api_source_from_context(playing_file, 'check_playing_new_url')
                        if self._can_use_tunein_api() and not self.tunein_station_id:
                            tunein_id = _tunein_extract_station_id(playing_file)
                            if tunein_id:
                                self.tunein_station_id = tunein_id
                                log_debug(f"TuneIn Station-ID aus Stream-URL: '{tunein_id}'")
                                self._reconcile_api_source('check_playing_tunein_id')
                        title = None
                        artist = None
                        album = None
                        WINDOW.clearProperty(_P.MBID)
                        WINDOW.clearProperty(_P.ALBUM)
                        WINDOW.clearProperty(_P.STATION)
                        self._apply_verified_source_hint(playing_file)
                        
                        # Basis-Informationen aus dem Player
                        try:
                            info_tag = self.player.getMusicInfoTag()
                            title = info_tag.getTitle()
                            artist = info_tag.getArtist()
                            album = info_tag.getAlbum()
                            
                            # Hole das Logo/Thumbnail vom aktuellen Item
                            # Prüfe verschiedene Quellen in Prioritätsreihenfolge
                            logo = None
                            player_art_candidates = {}
                            for source in ['Player.Art(poster)', 'Player.Icon', 'Player.Art(thumb)', 'MusicPlayer.Cover']:
                                player_art_candidates[source] = xbmc.getInfoLabel(source) or ''
                            
                            # 1. HÖCHSTE Priorität: ListItem.Icon (echtes Logo vom Addon, BEVOR Kodi es cached)
                            listitem_icon = xbmc.getInfoLabel('ListItem.Icon')
                            if self.is_real_logo(listitem_icon):
                                logo = listitem_icon
                                self.station_logo = logo
                                self._ensure_api_source_from_context(logo, 'check_playing_listitem_logo')
                                self._ensure_radiode_identity_from_value(logo, context='check_playing_listitem_logo')
                                if self._can_use_tunein_api() and not self.tunein_station_id:
                                    tunein_id = _tunein_extract_station_id(logo)
                                    if tunein_id:
                                        self.tunein_station_id = tunein_id
                                        log_info(f"TuneIn Station-ID aus Logo-URL: '{tunein_id}'")
                            
                            # 2. Fallback: Window-Property vom radio.de Addon
                            if not logo:
                                radiode_logo = WINDOW.getProperty(_P.RADIODE_LOGO)
                                if self.is_real_logo(radiode_logo):
                                    logo = radiode_logo
                                    self.station_logo = logo
                                    self._ensure_api_source_from_context(logo, 'check_playing_radiode_logo')
                                    self._ensure_radiode_identity_from_value(logo, context='check_playing_radiode_logo')
                            
                            # 3. Fallback: Player Art
                            if not logo:
                                for source, player_logo in player_art_candidates.items():
                                    if self.is_real_logo(player_logo):
                                        logo = player_logo
                                        self.station_logo = logo
                                        self._ensure_api_source_from_context(logo, f'check_playing_{source}')
                                        self._ensure_radiode_identity_from_value(logo, context=f'check_playing_{source}')
                                        if self._can_use_tunein_api() and not self.tunein_station_id:
                                            tunein_id = _tunein_extract_station_id(logo)
                                            if tunein_id:
                                                self.tunein_station_id = tunein_id
                                                log_info(f"TuneIn Station-ID aus Logo-URL: '{tunein_id}'")
                                        break

                            if not self.station_logo or not self.is_real_logo(self.station_logo):
                                log_debug("Kein Player-Logo, wird spaeter von API geholt")
                            
                            # Setze Logo (nur wenn echtes Logo, sonst Kodi-Fallback)
                            self.set_logo_safe()
                            if self.station_logo and self.is_real_logo(self.station_logo):
                                log_info(f"Logo gesetzt: {self.station_logo}")
                            else:
                                log_debug("Kein echtes Logo, nutze Kodi-Fallback")
                        except Exception as e:
                            log_debug(f"Fehler beim Lesen von InfoTag/Logo beim Stream-Start: {e}")
                        if album and (not self.station_logo or self.station_logo == 'DefaultAudio.png'):
                            try:
                                log_debug(f"Hole Station-Logo fuer: {album}")
                                # Suche Station in radio.de API
                                search_name = album
                                search_name = re.sub(r'\s*(inter\d+|mp3|aac|low|high|128|64|256).*$', '', search_name, flags=re.IGNORECASE)
                                search_name = search_name.strip()
                                
                                params = {'query': search_name, 'count': 5}
                                response = self.api_client.get(RADIODE_SEARCH_API_URL, params=params, timeout=5)
                                data = response.json()
                                
                                if 'playables' in data and len(data['playables']) > 0:
                                    # Nimm erste Station
                                    station = data['playables'][0]
                                    logo_url = station.get('logo300x300', '')
                                    if logo_url:
                                        self.station_logo = logo_url
                                        self.set_property_safe(_P.LOGO, logo_url)
                                        log_info(f"Station-Logo gefunden: {logo_url}")
                            except Exception as e:
                                log_debug(f"Fehler beim Holen des Station-Logos: {str(e)}")
                        
                        # Playing-Flag setzen
                        WINDOW.setProperty(_P.PLAYING, 'true')
                        
                        log_debug("=== STREAM GESTARTET - INITIAL STATE ===")
                        log_debug("RadioMonitor.Playing = true")
                        log_debug(f"RadioMonitor.Station = {WINDOW.getProperty(_P.STATION)}")
                        log_debug(f"RadioMonitor.Artist = {WINDOW.getProperty(_P.ARTIST)}")
                        log_debug(f"RadioMonitor.Title = {WINDOW.getProperty(_P.TITLE)}")
                        log_debug(f"RadioMonitor.Logo = {WINDOW.getProperty(_P.LOGO)}")
                        log_debug(f"RadioMonitor.Genre = {WINDOW.getProperty(_P.GENRE)}")
                        
                        log_info("========================================")

                        # ICY-Metadaten-Monitoring starten
                        self.start_metadata_monitoring(playing_file)

                        log_info(f"Stream erkannt: {playing_file}")
                else:
                    # Kein Stream - Properties löschen
                    if self.is_playing:
                        self.is_playing = False
                        self.current_url = None
                        self.stop_metadata_monitoring()
                        self.clear_properties()
            except Exception as e:
                log_error(f"Fehler beim Überprüfen des Players: {str(e)}")
        else:
            # Nichts wird abgespielt
            if self.is_playing:
                self.is_playing = False
                self.current_url = None
                self.stop_metadata_monitoring()
                self.clear_properties()
                log_info("Wiedergabe gestoppt")
                
    def run(self):
        """Haupt-Loop des Services"""
        # Skinfarben lesen und settings.xml mit aktuellem Farbdropdown aktualisieren
        _skin_colors.update_settings_colors()
        # Initial properties löschen
        self.clear_properties()
        
        # Haupt-Loop
        while not self.abortRequested():
            # Alle 2 Sekunden überprüfen
            if self.waitForAbort(2):
                break
                
            self.check_playing()
            self._sync_qf_result_property()
            self._tick_qf_request()
            self._update_timeout_remaining_property()
            
        # Cleanup beim Beenden
        self.stop_metadata_monitoring()
        self._flush_station_profiles()
        try:
            if self._profile_store is not None:
                self._profile_store.close(flush=bool(self._persist_data))
        except Exception as e:
            log_debug(f"Station profile store close fehlgeschlagen: {e}")
        try:
            if self.analysis_store is not None:
                self.analysis_store.close()
        except Exception as e:
            log_debug(f"Analysis store close fehlgeschlagen: {e}")
        self.api_client.close()
        self.clear_properties()
        log_info("Service beendet")

if __name__ == '__main__':
    monitor = RadioMonitor()
    monitor.run()
