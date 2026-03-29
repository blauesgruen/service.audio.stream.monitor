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
    ADDON, ADDON_ID, ADDON_NAME, ADDON_VERSION,
    RADIODE_SEARCH_API_URL, RADIODE_NOWPLAYING_API_URL, RADIODE_DETAILS_API_URL,
    TUNEIN_DESCRIBE_API_URL, TUNEIN_TUNE_API_URL,
    DEFAULT_HTTP_HEADERS, INVALID_METADATA_VALUES,
    SONG_TIMEOUT_FALLBACK_S, SONG_TIMEOUT_EARLY_CLEAR_S,
    API_NOW_REFRESH_INTERVAL_S, PLAYER_BUFFER_SETTLE_S, PLAYER_BUFFER_MAX_WAIT_S,
    API_METADATA_POLL_INTERVAL_S, MUSICPLAYER_FALLBACK_POLL_INTERVAL_S,
    ANALYSIS_ENABLED, ANALYSIS_EVENTS_FILENAME, ANALYSIS_MAX_EVENTS, ANALYSIS_FLUSH_INTERVAL_S,
    STATION_PROFILE_DIRNAME, STATION_PROFILE_OBSERVE_INTERVAL_S, STATION_PROFILE_SAVE_INTERVAL_S,
    SOURCE_POLICY_WINDOW, SOURCE_POLICY_SWITCH_MARGIN, SOURCE_POLICY_SINGLE_CONFIRM_POLLS,
    STARTUP_SOURCE_QUALIFY_WINDOW_S, STARTUP_API_ONLY_STABLE_POLLS,
    RADIODE_PLUGIN_IDS as _RADIODE_PLUGIN_IDS,
    TUNEIN_PLUGIN_IDS as _TUNEIN_PLUGIN_IDS,
    MB_WINNER_MIN_SCORE as _MB_WINNER_MIN_SCORE,
    MB_WINNER_MIN_COMBINED as _MB_WINNER_MIN_COMBINED,
    MP_TRUST_MAX_MISMATCHES as _MP_TRUST_MAX_MISMATCHES,
    MP_DECISION_ENABLED as _MP_DECISION_ENABLED,
    TRIGGER_TITLE_CHANGE as _TRIGGER_TITLE_CHANGE,
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
from raw_sources import RawSourceLabels, snapshot_getters
from analysis_events import AnalysisEventStore, new_trace_id
from musicbrainz import (
    identify_artist_title_via_musicbrainz as _identify_artist_title_via_musicbrainz,
    musicbrainz_query_artist_info as _musicbrainz_query_artist_info,
    musicbrainz_query_recording as _musicbrainz_query_recording,
    mb_similarity as _mb_similarity,
    _mb_cache,
)
from radiode import parse_radiode_api_title as _parse_radiode_api_title
from metadata import (
    extract_stream_title as _extract_stream_title,
    parse_stream_title_simple as _parse_stream_title_simple,
    parse_stream_title_complex as _parse_metadata_complex,
    get_last_separator_variant as _get_last_separator_variant
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
                        log_info(f"Plugin-Slug aus iconimage: '{slug}'")
            elif 'plugin.audio.radiode' in playing_file:
                self.radio_monitor._set_api_source(self.radio_monitor.API_SOURCE_RADIODE)
                log_debug("radio.de Addon erkannt (plugin.audio.radiode)")
            elif 'plugin.audio.tunein2017' in playing_file:
                self.radio_monitor._set_api_source(self.radio_monitor.API_SOURCE_TUNEIN)
                tunein_id = self.radio_monitor._extract_tunein_station_id(playing_file)
                if tunein_id:
                    self.radio_monitor.tunein_station_id = tunein_id
                    xbmc.log(f"[{ADDON_NAME}] TuneIn Station-ID aus Plugin-URL: '{tunein_id}'", xbmc.LOGINFO)

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
                xbmc.log(f"[{ADDON_NAME}] Video gestartet - lösche Radio-Properties sofort", xbmc.LOGINFO)
                self.radio_monitor.is_playing = False
                self.radio_monitor.current_url = None
                self.radio_monitor.stop_metadata_monitoring()
                self.radio_monitor.clear_properties()
                return

            if self.isPlayingAudio():
                playing_file = self.getPlayingFile()

                # Lokale Datei → Radio-Properties sofort löschen
                if not (playing_file.startswith('http://') or playing_file.startswith('https://')):
                    xbmc.log(f"[{ADDON_NAME}] Lokale Datei gestartet - lösche Radio-Properties sofort", xbmc.LOGINFO)
                    self.radio_monitor.is_playing = False
                    self.radio_monitor.current_url = None
                    self.radio_monitor.stop_metadata_monitoring()
                    self.radio_monitor.clear_properties()
                    return

                # HTTP/HTTPS Audio-Stream → SOFORT Logo vom ListItem lesen
                listitem_icon = xbmc.getInfoLabel('ListItem.Icon')
                if listitem_icon and self.radio_monitor.is_real_logo(listitem_icon):
                    self.radio_monitor.station_logo = listitem_icon
                    xbmc.log(f"[{ADDON_NAME}] Logo SOFORT beim Start erfasst: {listitem_icon}", xbmc.LOGINFO)
                else:
                    log_debug(f"ListItem.Icon beim Start: {listitem_icon}")
        except Exception as e:
            xbmc.log(f"[{ADDON_NAME}] Fehler in onAVStarted: {str(e)}", xbmc.LOGERROR)

class RadioMonitor(xbmc.Monitor):
    """
    Hauptklasse für das Monitoring und die Verwaltung von Radio-Streams, Metadaten und Player-Events.
    Verantwortlich für das Setzen und Löschen von Properties, das Aktualisieren von Metadaten und das Handling von API-Fallbacks.
    """
    API_SOURCE_NONE = ''
    API_SOURCE_RADIODE = 'radiode'
    API_SOURCE_TUNEIN = 'tunein'
    STREAM_SOURCE_FAMILIES = ('api', 'icy')
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
    MP_GENERIC_HOLD_MAX_S = 120.0

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
        self.analysis_enabled = bool(ANALYSIS_ENABLED)
        self._analysis_seq = 0
        self.analysis_store = self._init_analysis_store() if self.analysis_enabled else None
        self.mp_decision_enabled = bool(self.MP_DECISION_ENABLED)
        
        # Event-Handler für Player-Events
        self.player_monitor = PlayerMonitor(self)
        
        xbmc.log(f"[{ADDON_NAME}] Service gestartet", xbmc.LOGINFO)

    def _reset_api_context(self):
        """Setzt API-relevanten Zustand zentral zurück."""
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
            xbmc.log(
                f"[{ADDON_NAME}] API-Source automatisch erkannt "
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

    def _is_song_pair(self, pair):
        return bool(pair and pair[0] and pair[1])

    def _is_generic_metadata_text(self, text, station_name=''):
        text_l = str(text or '').strip().lower()
        if not text_l:
            return False
        station_l = (station_name or '').strip().lower()
        if station_l and station_l in text_l:
            return True
        generic_tokens = (
            'wir sind',
            'nachrichten',
            'verkehr',
            'wetter',
            'news',
            'jingle',
        )
        return any(token in text_l for token in generic_tokens)

    def _is_generic_song_pair(self, pair, station_name=''):
        if not self._is_song_pair(pair):
            return False
        a_l = str(pair[0] or '').strip().lower()
        t_l = str(pair[1] or '').strip().lower()
        return (
            self._is_generic_metadata_text(a_l, station_name)
            or self._is_generic_metadata_text(t_l, station_name)
            or self._is_generic_metadata_text(f"{a_l} - {t_l}", station_name)
        )

    def _has_non_generic_song_pair(self, pair, station_name=''):
        return self._is_song_pair(pair) and not self._is_generic_song_pair(pair, station_name)

    def _is_generic_stream_title(self, stream_title, station_name=''):
        return self._is_generic_metadata_text(stream_title, station_name)

    def _filter_non_generic_song_pairs(self, pairs, station_name=''):
        return [p for p in (pairs or []) if self._has_non_generic_song_pair(p, station_name)]

    def _sanitize_musicplayer_pair(self, pair, station_name=''):
        if self._is_generic_song_pair(pair, station_name):
            return ('', '')
        return pair

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
            self._close_station_profile_session()
            self._active_station_profile_key = key
            self._station_profile_session = self._profile_store.start_session(key, station_name)
            self._station_profile_policy_enabled = False
            self._active_policy_profile = {}
            self.source_policy.clear_station_profile()
            log_debug(f"Station-Profil Session gestartet: key='{key}'")

        if enable_policy and not key_changed and not self._station_profile_policy_enabled:
            policy_profile = self._profile_store.get_policy_profile(key)
            self.source_policy.apply_station_profile(policy_profile)
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
        context['icy_is_song'] = bool(
            self._is_song_pair(context.get('current_icy_pair'))
            and not self._is_generic_song_pair(context.get('current_icy_pair'), station_name)
        )
        self._station_profile_session.observe(observation, context)
        self._profile_store.flush_if_due(min_interval_s=STATION_PROFILE_SAVE_INTERVAL_S)

    def _close_station_profile_session(self):
        if self._profile_store is None or self._station_profile_session is None:
            return
        try:
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
        self._close_station_profile_session()
        self._active_station_profile_key = ''
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
        
    def _start_song_timeout(self, duration_ms):
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

        log_debug(f"Song-Timeout abgelaufen ({self._song_timeout:.0f}s) - loesche Song-Properties")

        if enable_api_block and last_song_key[0] and last_song_key[1]:
            self._api_timeout_block_key = (last_song_key[0], last_song_key[1])
            log_debug(
                f"API-Block bis Songwechsel aktiviert: "
                f"'{last_song_key[0]} - {last_song_key[1]}'"
            )

        WINDOW.clearProperty(_P.ARTIST)
        WINDOW.clearProperty(_P.TITLE)
        WINDOW.clearProperty(_P.ALBUM)
        WINDOW.clearProperty(_P.ALBUM_DATE)
        WINDOW.clearProperty(_P.MBID)
        WINDOW.clearProperty(_P.FIRST_REL)
        WINDOW.clearProperty(_P.BAND_FORM)
        WINDOW.clearProperty(_P.BAND_MEM)
        WINDOW.clearProperty(_P.GENRE)
        self._reset_song_timeout_state(clear_debug=True)
        return True

    def clear_properties(self):
        """Löscht alle Radio-Properties"""
        if self._profile_store is not None and self._station_profile_session is not None:
            self._flush_station_profiles()

        # Reset Logo und API-Kontext
        self.station_logo = None
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
        self._mp_generic_hold_active = False
        self._mp_generic_hold_since_ts = 0.0
        self._mp_generic_hold_timed_out = False
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

    def set_property_safe(self, key, value):
        """Setzt eine Window-Property nur wenn der Wert nicht leer ist."""
        if value:
            WINDOW.setProperty(key, str(value))
    
    def is_real_logo(self, url):
        """Prüft ob es ein echtes Logo ist (keine Kodi-Fallbacks)"""
        if not url:
            return False
        invalid = ['DefaultAudio', 'DefaultAlbum', 'no_image', 'no-image', 'default.png', 'Default']
        return not any(x in str(url) for x in invalid)
    
    def set_logo_safe(self):
        """Setzt Logo-Property nur wenn echtes Logo vorhanden, sonst Kodi-Fallback"""
        if self.station_logo and self.is_real_logo(self.station_logo):
            self.set_property_safe(_P.LOGO, self.station_logo)
        else:
            # Kein echtes Logo → Property leer lassen (Kodi nutzt automatisch Fallback)
            WINDOW.clearProperty(_P.LOGO)

    def _compose_song_label(self, artist=None, title=None):
        a = (artist or '').strip()
        t = (title or '').strip()
        if a and t:
            return f"{a} - {t}"
        return t or a

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

                    return station_name

            if self._can_use_tunein_api():
                self.use_api_fallback = True
                tunein_id = self._extract_tunein_station_id(url)
                if tunein_id:
                    self.tunein_station_id = tunein_id
                    log_debug(f"TuneIn Stream erkannt, Station-ID aus URL: '{tunein_id}'")
                else:
                    log_debug("TuneIn Stream erkannt, aber keine Station-ID in URL gefunden")
        except Exception as e:
            log_debug(f"Fehler bei URL-Analyse fuer API-Fallback: {str(e)}")
        return None

    def _extract_tunein_station_id(self, text):
        """
        Extrahiert eine TuneIn-ID (z.B. s24878, t109814382) aus Plugin- oder Stream-URLs.
        Unterstützt auch verschachtelt URL-encodete fparams.
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

    def _parse_tunein_nowplaying_candidate(self, value, station_name=None):
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
            artist, title = self.parse_stream_title_simple(candidate)
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

    def _extract_tunein_from_json(self, payload, station_name=None):
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

        for candidate in candidates:
            artist, title = self._parse_tunein_nowplaying_candidate(candidate, station_name)
            if artist or title:
                return artist, title
        return None, None

    def _extract_tunein_from_text(self, text, station_name=None):
        """Fallback-Parser für XML/Plain-Text Antworten aus TuneIn OPML APIs."""
        if not text:
            return None, None

        patterns = [
            r'playing="([^"]+)"',
            r'subtext="([^"]+)"',
            r'"playing"\s*:\s*"([^"]+)"',
            r'"subtitle"\s*:\s*"([^"]+)"',
            r'"subtext"\s*:\s*"([^"]+)"',
        ]

        for pattern in patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                candidate = match.group(1).strip()
                artist, title = self._parse_tunein_nowplaying_candidate(candidate, station_name)
                if artist or title:
                    return artist, title
        return None, None

    def get_tunein_api_nowplaying(self, station_name=None):
        """Holt aktuelle Song-Info für TuneIn-Streams über OPML-Endpunkte."""
        station_id = self.tunein_station_id
        if not station_id:
            return None, None

        endpoints = [
            (TUNEIN_DESCRIBE_API_URL, {'id': station_id, 'render': 'json'}),
            (TUNEIN_TUNE_API_URL, {'id': station_id, 'render': 'json'}),
            (TUNEIN_TUNE_API_URL, {'id': station_id, 'render': 'json', 'formats': 'mp3,aac,ogg,hls'}),
        ]

        for endpoint, params in endpoints:
            try:
                response = self.api_client.get(endpoint, params=params, timeout=5)
                if response.status_code != 200:
                    log_debug(f"TuneIn API Status {response.status_code} fuer {endpoint}")
                    continue

                try:
                    payload = response.json()
                except Exception:
                    payload = None

                if payload is not None:
                    self._debug_log_api_raw('tunein.json', payload)
                    artist, title = self._extract_tunein_from_json(payload, station_name)
                    if artist or title:
                        log_info(f"OK TuneIn API: {artist} - {title}")
                        return artist, title

                self._debug_log_api_raw('tunein.text', response.text)
                artist, title = self._extract_tunein_from_text(response.text, station_name)
                if artist or title:
                    xbmc.log(f"[{ADDON_NAME}] OK TuneIn API (Text): {artist} - {title}", xbmc.LOGINFO)
                    return artist, title
            except Exception as e:
                log_debug(f"Fehler bei TuneIn API Abfrage ({endpoint}): {e}")

        return None, None
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
                xbmc.log(f"[{ADDON_NAME}] OK radio.de API: {artist} - {title}", xbmc.LOGINFO)
                self._set_api_nowplaying_label(artist, title)
                return artist, title

        # 2. TuneIn API nur wenn Source=TuneIn
        if self._can_use_tunein_api():
            artist, title = self.get_tunein_api_nowplaying(station_name)
            if artist or title:
                xbmc.log(f"[{ADDON_NAME}] OK TuneIn API: {artist} - {title}", xbmc.LOGINFO)
                self._set_api_nowplaying_label(artist, title)
                return artist, title
        # 3. Optionaler Fallback: Kodi Player InfoTags (nur mit MP-Entscheidung)
        WINDOW.clearProperty(_P.API_NOW)
        if not self.mp_decision_enabled:
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
        if _NUMERIC_ID_RE.match(a) or _NUMERIC_ID_RE.match(t):
            return None, None
        return a, t

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
                        xbmc.log(
                            f"[{ADDON_NAME}] MusicPlayer-Kandidat via InfoLabel bevorzugt: "
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
        initial_source_pending
    ):
        """
        Zentrale Trigger-Erkennung ueber das modulare Source-Policy-Modell.
        """
        reasons = {
            'title': self.TRIGGER_TITLE_CHANGE,
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
            reasons=reasons
        )
        self._last_policy_context = {
            'station_name': station_name,
            'current_mp_pair': current_mp_pair if self._is_song_pair(current_mp_pair) else ('', ''),
            'current_api_pair': current_api_pair if self._is_song_pair(current_api_pair) else ('', ''),
            'current_icy_pair': current_icy_pair if self._is_song_pair(current_icy_pair) else ('', ''),
            'stream_title_changed': bool(stream_title_changed),
            'triggered': bool(changed),
            'trigger_reason': reason if changed else ''
        }
        scores = self.source_policy.debug_scores()
        if not self.mp_decision_enabled and isinstance(scores, dict):
            scores = {k: v for k, v in scores.items() if k != 'musicplayer'}
        self._policy_preferred_source = preferred or ''
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
        """Normalisiert eine Source auf ihre Familie (api/icy/musicplayer)."""
        s = str(source or '')
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
        if locked_family not in ('musicplayer', 'api', 'icy'):
            return candidates

        has_locked_data = False
        if locked_family == 'musicplayer':
            has_locked_data = bool(mp_pairs)
        elif locked_family == 'api':
            has_locked_data = bool(api_candidate and api_candidate[0] and api_candidate[1])
        elif locked_family == 'icy':
            has_locked_data = bool(icy_pairs)

        if not has_locked_data:
            xbmc.log(
                f"[{ADDON_NAME}] Source-Lock geloest: '{locked_family}' ohne valide Daten -> Fallback aktiv")
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

    def _resolve_mb_zero_with_source_lock(self, locked_source, mp_pairs, api_candidate, icy_pairs):
        """
        MB=0 Fallback fuer source-locked Auswertungen:
        - Bei aktivem Lock darf keine andere Quelle den Lock uebersteuern.
        Rueckgabe: (source_name, (artist, title)) oder ('', ('', '')).
        """
        locked_source_name = str(locked_source or '')
        locked_family = self._source_family(locked_source_name)
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
        if locked_family == 'icy' and icy_pairs:
            return locked_source_name, icy_pairs[0]
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
        if source_family in ('musicplayer', 'icy', 'api'):
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
        score, mb_artist, mb_title, mbid, mb_album, mb_album_date, mb_first_release, mb_duration_ms = \
            _musicbrainz_query_recording(title, artist)
        artist_sim = _mb_similarity(artist, mb_artist) if mb_artist else 0.0
        title_sim = _mb_similarity(title, mb_title) if mb_title else 0.0
        combined = float(score) * ((artist_sim + title_sim) / 2.0)
        return {
            'source': source,
            'input_artist': artist,
            'input_title': title,
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
            if ev['score'] >= self.MB_WINNER_MIN_SCORE and ev['combined'] >= self.MB_WINNER_MIN_COMBINED
        ]
        if not valid:
            log_debug(
                f"MB-Winner: kein Kandidat über Schwellwert "
                f"(min_score={self.MB_WINNER_MIN_SCORE}, min_combined={self.MB_WINNER_MIN_COMBINED:.1f})")
            return None, evaluations

        def _source_rank(source):
            s = str(source or '')
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
                ev['combined'],
                ev['score'],
                _source_rank(ev.get('source'))
            )
        )
        log_info(
            f"MB-Winner: source={winner['source']} "
            f"('{winner['mb_artist']} - {winner['mb_title']}'), "
            f"score={winner['score']}, combined={winner['combined']:.1f}"
        )
        return winner, evaluations
    
    def get_radiode_api_nowplaying(self, station_name):
        """Holt aktuelle Song-Info direkt von der radio.de API"""
        try:
            # Slug-Quelle: Plugin-URL hat Priorität, Logo-URL als Fallback
            slug = self.plugin_slug
            if not slug and self.station_logo:
                logo_match = re.search(r'radio-assets\.com/\d+/([^./?]+)', self.station_logo)
                if logo_match:
                    slug = logo_match.group(1)

            if slug:
                log_debug(f"Station-Slug: '{slug}' (plugin={bool(self.plugin_slug)})")
                try:
                    det_response = self.api_client.get(RADIODE_DETAILS_API_URL, params={'stationIds': slug}, timeout=5)
                    if det_response.status_code == 200:
                        det_data = det_response.json()
                        if isinstance(det_data, list) and len(det_data) > 0:
                            proper_name = det_data[0].get('name', '')
                            if proper_name:
                                self.set_property_safe(_P.STATION, proper_name)
                            det_logo = det_data[0].get('logo300x300', '')
                            if det_logo and not self.station_logo:
                                self.station_logo = det_logo
                                self.set_logo_safe()
                except Exception as e:
                    log_debug(f"Fehler bei Details-API: {e}")

                try:
                    np_response = self.api_client.get(RADIODE_NOWPLAYING_API_URL, params={'stationIds': slug}, timeout=5)
                    if np_response.status_code == 200:
                        np_data = np_response.json()
                        self._debug_log_api_raw('radiode.now_playing.slug', np_data)
                        if isinstance(np_data, list) and len(np_data) > 0:
                            full_title = np_data[0].get('title', '')
                            if full_title:
                                artist, title = _parse_radiode_api_title(full_title, station_name)
                                if artist or title:
                                    xbmc.log(f"[{ADDON_NAME}] OK now-playing via Slug: {artist} - {title}", xbmc.LOGINFO)
                                    return artist, title
                        log_debug("Slug-Abfrage ohne Ergebnis - weiter mit Suche")
                except Exception as e:
                    log_debug(f"Fehler bei now-playing via Slug: {e}")

            # Bereinige den Sendernamen FÜR DIE SUCHE
            search_name = station_name
            
            # Entferne technische Suffixe
            search_name = re.sub(r'\s*(inter\d+|mp3|aac|low|high|128|64|256).*$', '', search_name, flags=re.IGNORECASE)
            search_name = re.sub(r'\s*-\s*[A-Z]{2,3}\s*$', '', search_name)  # z.B. " - RK"
            
            # Entferne spezielle Zusätze die die Suche stören
            search_name = re.sub(r'\s*-\s*100%.*$', '', search_name, flags=re.IGNORECASE)  # "- 100% Deutsch"
            search_name = re.sub(r'\s*91\.4.*$', '', search_name, flags=re.IGNORECASE)  # "91.4"
            search_name = re.sub(r'\s*-\s*\d+\.\d+.*$', '', search_name)  # Frequenzen wie "- 91.4"
            
            search_name = search_name.strip()
            
            log_debug(f"Suche radio.de API mit: '{search_name}' (Original: '{station_name}')")
            
            params = {'query': search_name, 'count': 20}
            response = self.api_client.get(RADIODE_SEARCH_API_URL, params=params, timeout=5)
            if response.status_code != 200:
                xbmc.log(f"[{ADDON_NAME}] radio.de API: ungültige Antwort (Status {response.status_code})", xbmc.LOGWARNING)
                return None, None
            data = response.json()
            
            log_debug(f"Search API: {data.get('totalCount', 0)} Treffer")
            
            # Schritt 1: Stationsname bereinigen und radio.de API durchsuchen
            if 'playables' in data and len(data['playables']) > 0:
                # Suche die beste Übereinstimmung
                best_match = None
                best_match_score = 0
                
                # Normalisiere beide Namen für Vergleich
                search_normalized = search_name.lower().replace('-', ' ').replace('_', ' ').strip()
                
                for station in data['playables'][:20]:  # Prüfe die ersten 20 Treffer
                    station_found = station.get('name', '')
                    station_normalized = station_found.lower().replace('-', ' ').replace('_', ' ').strip()
                    
                    # Exakter Match (Priorität)
                    if station_normalized == search_normalized:
                        best_match = station
                        best_match_score = 1000  # Höchste Priorität
                        log_debug(f"EXAKTER MATCH gefunden: '{station_found}'")
                        break
                    
                    # Substring-Match (Station enthält Suchbegriff)
                    if search_normalized in station_normalized:
                        score = 100 + len(search_normalized)  # Je länger der Match, desto besser
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
                            log_debug(f"Wort-Match: '{station_found}' - Score: {score} (Woerter: {matching_words})")
                
                if best_match and best_match_score > 0:
                    station_found = best_match.get('name', '')
                    station_id = best_match.get('id', '')
                    station_logo = best_match.get('logo300x300', '')  # Logo aus API

                    # Speichere Logo für spätere Verwendung
                    if station_logo:
                        self.station_logo = station_logo
                        self.set_logo_safe()
                        xbmc.log(f"[{ADDON_NAME}] Station-Logo aus API: {station_logo}", xbmc.LOGINFO)

                    log_debug(f"Beste Uebereinstimmung: '{station_found}' (Score: {best_match_score}, ID: {station_id})")
                    
                    # Schritt 2: Station-ID für now-playing API verwenden
                    if station_id:
                        log_debug(f"Hole Now-Playing von: {RADIODE_NOWPLAYING_API_URL}?stationIds={station_id}")
                        
                        try:
                            params = {'stationIds': station_id}
                            np_response = self.api_client.get(RADIODE_NOWPLAYING_API_URL, params=params, timeout=5)
                            if np_response.status_code == 200:
                                np_data = np_response.json()
                                self._debug_log_api_raw('radiode.now_playing.search', np_data)
                                
                                # Response ist ein Array: [{"title":"ARTIST - TITLE","stationId":"..."}]
                                if isinstance(np_data, list) and len(np_data) > 0:
                                    track_info = np_data[0]
                                    full_title = track_info.get('title', '')
                                    
                                    log_debug(f"Empfangener Titel: '{full_title}'")
                                    
                                    if full_title:
                                        artist, title = _parse_radiode_api_title(full_title, station_name)
                                        if artist is not None or title is not None:
                                            if artist and title:
                                                xbmc.log(f"[{ADDON_NAME}] OK now-playing API erfolgreich: {artist} - {title}", xbmc.LOGINFO)
                                                return artist, title
                                            if title:
                                                xbmc.log(f"[{ADDON_NAME}] OK now-playing API erfolgreich (nur Title): {title}", xbmc.LOGINFO)
                                                return None, title
                                    else:
                                        log_debug(f"Titel-Format unbekannt: '{full_title}'")
                                else:
                                    log_debug("Leere now-playing Response")
                            else:
                                log_debug(f"now-playing API Fehler: {np_response.status_code}")
                        except Exception as e:
                            xbmc.log(f"[{ADDON_NAME}] Fehler bei now-playing API: {str(e)}", xbmc.LOGWARNING)

                    else:
                        log_debug("Keine Station-ID gefunden")
                else:
                    log_debug("Kein Match gefunden (Score zu niedrig)")
            else:
                log_debug(f"Keine Treffer fuer '{search_name}'")
                        
        except Exception as e:
            xbmc.log(f"[{ADDON_NAME}] Fehler bei radio.de API Abfrage: {str(e)}", xbmc.LOGWARNING)
        
        return None, None
    
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
                        if artist and title:
                            mb_artist, mb_title, mb_album, mb_album_date, mbid, mb_first_release, uncertain, duration_ms = _identify_artist_title_via_musicbrainz(artist, title)
                            if uncertain:
                                mbid = ''
                                duration_ms = 0
                            else:
                                album = mb_album
                                album_date = mb_album_date
                                first_release = mb_first_release
                                if mb_artist and mb_title and (
                                    _mb_similarity(mb_artist, artist) < 0.8 or _mb_similarity(mb_title, title) < 0.8
                                ):
                                    # Nur MBID/Album nutzen, wenn MB den API-Titel plausibel bestätigt.
                                    mbid = ''
                                    album = ''
                                    album_date = ''
                                    first_release = ''
                                    duration_ms = 0
                        
                        if artist:
                            # Reihenfolge: MBID und Title vor Artist setzen.
                            # AS lauscht auf RadioMonitor.Artist als Trigger und liest
                            # danach sofort RadioMonitor.MBID – daher muss MBID bereits
                            # gesetzt sein wenn Artist den Trigger auslöst.
                            self.set_property_safe(_P.TITLE, title)
                            self.set_property_safe(_P.STREAM_TTL, f"{artist} - {title}")
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
                            self.set_property_safe(_P.ARTIST, artist)
                            xbmc.log(f"[{ADDON_NAME}] API Update: {artist} - {title}", xbmc.LOGINFO)
                            if mbid and artist:
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

                            # Aktualisiere Kodi Player Metadaten
                            logo = WINDOW.getProperty(_P.LOGO)
                            self.update_player_metadata(artist, title, album if album else station_name, logo if logo else None, mbid if mbid else None)
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
                            xbmc.log(f"[{ADDON_NAME}] API Update: {title}", xbmc.LOGINFO)
                            
                            # Aktualisiere Kodi Player Metadaten
                            logo = WINDOW.getProperty(_P.LOGO)
                            self.update_player_metadata(None, title, station_name, logo if logo else None, None)

                        # Song-Timeout: Timer (neu) starten sobald ein Titel erkannt wurde.
                        self._start_song_timeout(duration_ms)

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

                    xbmc.log(f"[{ADDON_NAME}] MusicPlayer-Fallback: Artist='{mp_artist}', Title='{mp_title}'", xbmc.LOGINFO)

                    # MusicBrainz-Lookup
                    _, mb_artist, mb_title, mbid, mb_album, mb_album_date, mb_first_release, duration_ms = \
                        _musicbrainz_query_recording(mp_title, mp_artist)

                    artist = mb_artist or mp_artist
                    title = mb_title or mp_title

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
                        xbmc.log(f"[{ADDON_NAME}] MusicPlayer-Fallback gesetzt: Artist='{artist}', Title='{title}', MBID='{mbid}'", xbmc.LOGINFO)
                    else:
                        WINDOW.clearProperty(_P.ARTIST)
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
                    self._start_song_timeout(duration_ms)

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
            else:
                log_debug("Kein icy-name im Header")
            
            if icy_genre:
                log_debug(f"Genre: {icy_genre}")
            
            # Metaint - Position der Metadaten im Stream
            metaint = response.headers.get('icy-metaint')
            if not metaint:
                xbmc.log(f"[{ADDON_NAME}] Kein icy-metaint Header gefunden - Stream sendet keine ICY-Metadaten", xbmc.LOGWARNING)
                self._setup_api_fallback_from_url(url)
                response.close()
                return None

            metaint = int(metaint)
            log_debug(f"MetaInt: {metaint}")
            
            return {'metaint': metaint, 'response': response, 'station': station_name, 'genre': icy_genre}
            
        except Exception as e:
            xbmc.log(f"[{ADDON_NAME}] Fehler beim Abrufen der ICY-Metadaten: {str(e)}", xbmc.LOGERROR)
            self._setup_api_fallback_from_url(url)
            return None
            
    def extract_stream_title(self, metadata_raw):
        """Extrahiert den StreamTitle aus den rohen Metadaten (Nutzt zentrales metadata Modul)"""
        return _extract_stream_title(metadata_raw)
        
    def parse_stream_title(self, stream_title, station_name=None, stream_url=None):
        """
        Trennt Artist und Title aus dem ICY-StreamTitle.
        Priorität:
        1. Kandidaten aus API + ICY bilden
        2. MusicBrainz bewertet jeden Kandidaten (Score + Similarity)
        3. Kandidat mit bestem MB-Combined gewinnt
        4. Falls MB nichts belastbares liefert: bestehender ICY-Fallback
        """
        # Parse-Zyklus starten, ohne die zuletzt gesetzte Quelle sofort zu loeschen.
        # Sonst flackert RadioMonitor.Source zwischen '' und dem finalen Gewinner.
        self._last_decision_source = ''
        self._last_decision_pair = ('', '')
        invalid = INVALID_METADATA_VALUES + ["", station_name]
        artist, title, is_von, has_multi = _parse_metadata_complex(stream_title, station_name)

        # --- Kandidaten sammeln ---
        candidates = []
        api_candidate = (None, None)
        api_changed = False
        mp_direct = (None, None)
        mp_swapped = (None, None)
        mp_pairs = []

        # MusicPlayer-Kandidaten optional lesen (zentrale Abschaltung ueber MP_DECISION_ENABLED).
        if self.mp_decision_enabled:
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
        if api_candidate_available and not self._is_api_source_allowed():
            self._log_api_source_blocked('parse_stream_title_api_first')
        if api_candidate_available and self._is_api_source_allowed():
            api_artist, api_title = self.get_nowplaying_from_apis(station_name, stream_url)
            api_artist, api_title = self._normalize_song_candidate(api_artist, api_title, invalid)
            if api_artist and api_title:
                api_key = (api_artist, api_title)
                if self._api_timeout_block_key and api_key == self._api_timeout_block_key:
                    xbmc.log(
                        f"[{ADDON_NAME}] API-Kandidat geblockt nach Timeout: '{api_artist} - {api_title}'")
                else:
                    if self._api_timeout_block_key != ('', '') and api_key != self._api_timeout_block_key:
                        log_info(f"API-Song geaendert, Timeout-Block aufgehoben: '{api_artist} - {api_title}'")
                        self._api_timeout_block_key = ('', '')
                    candidates.append({'source': 'api', 'artist': api_artist, 'title': api_title})
                    if api_artist != api_title:
                        s_artist, s_title = self._normalize_song_candidate(api_title, api_artist, invalid)
                        if s_artist and s_title:
                            candidates.append({'source': 'api_swapped', 'artist': s_artist, 'title': s_title})
                    api_candidate = (api_artist, api_title)
                    api_changed = (self._last_seen_api_key != ('', '') and api_key != self._last_seen_api_key)
                    self._last_seen_api_key = api_key

        # ICY-Kandidaten (direkt + ggf. swapped)
        icy_artist, icy_title = self._normalize_song_candidate(artist, title, invalid)
        if icy_artist and icy_title:
            candidates.append({'source': 'icy', 'artist': icy_artist, 'title': icy_title})
            if not is_von and icy_artist != icy_title:
                s_artist, s_title = self._normalize_song_candidate(icy_title, icy_artist, invalid)
                if s_artist and s_title:
                    candidates.append({'source': 'icy_swapped', 'artist': s_artist, 'title': s_title})

        icy_candidate_pairs = [
            (c.get('artist'), c.get('title'))
            for c in candidates
            if str(c.get('source', '')).startswith('icy')
        ]
        mp_candidates_allowed = False
        if self.mp_decision_enabled:
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
            if self.mp_decision_enabled:
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
                winner['mb_artist'],
                winner['mb_title'],
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
                if self.mp_decision_enabled:
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
                    fb_winner['mb_artist'],
                    fb_winner['mb_title'],
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
        if prioritize_stream and stream_candidates and evaluations and all(ev.get('score', 0) == 0 for ev in evaluations):
            # MB=0 für die Stream-Quelle: API/ICY intern entscheiden.
            # MusicPlayer wird danach nur zum Abgleich/Trust genutzt.
            icy_pairs = {
                (ev.get('input_artist'), ev.get('input_title'))
                for ev in evaluations
                if str(ev.get('source', '')).startswith('icy')
            }
            locked_source_family, locked_source_pair = self._resolve_mb_zero_with_source_lock(
                locked_source,
                [],
                api_candidate,
                list(icy_pairs)
            )
            if locked_source_family and locked_source_pair[0] and locked_source_pair[1]:
                xbmc.log(
                    f"[{ADDON_NAME}] MB score=0 fuer alle Kandidaten, Source-Lock='{locked_source_family}' "
                    f"-> nutze gelockte Quelle: '{locked_source_pair[0]} - {locked_source_pair[1]}'",
                    xbmc.LOGINFO
                )
                self._set_last_song_decision(
                    locked_source_family,
                    locked_source_pair[0],
                    locked_source_pair[1]
                )
                if self.mp_decision_enabled:
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
                xbmc.log(
                    f"[{ADDON_NAME}] MB score=0 für alle Kandidaten, {reason} -> nutze API: "
                    f"'{api_candidate[0]} - {api_candidate[1]}'",
                    xbmc.LOGINFO
                )
                self._set_last_song_decision('api', api_candidate[0], api_candidate[1])
                if self.mp_decision_enabled:
                    self._log_musicplayer_comparison('api', api_candidate, mp_pairs)
                    self._update_musicplayer_trust_after_decision('api', api_candidate, mp_pairs)
                return api_candidate[0], api_candidate[1], '', '', '', '', 0
            xbmc.log(
                f"[{ADDON_NAME}] MB score=0 für alle Kandidaten, keine belastbaren Songdaten -> "
                f"nutze nur Station/StreamTitle")
            self._set_last_song_decision('', None, None)
            return None, None, '', '', '', '', 0

        # --- ICY-Analyse (bestehender Fallback) ---
        if not artist and not title:
            self._set_last_song_decision('', None, None)
            return None, None, '', '', '', '', 0

        # MusicBrainz zur Verifikation und Vervollständigung
        mb_first_release = ''
        duration_ms = 0
        
        if has_multi:
            # Mehrfaches ' - ' -> last-separator Variante prüfen
            alt_p1, alt_p2 = _get_last_separator_variant(stream_title)
            xbmc.log(f"[{ADDON_NAME}] MusicBrainz: prüfe last-separator Variante: Title='{alt_p1}', Artist='{alt_p2}'", xbmc.LOGINFO)
            mb_artist, mb_title, mb_album, mb_album_date, mbid, mb_first_release, uncertain, duration_ms = _identify_artist_title_via_musicbrainz(alt_p1, alt_p2)
            if uncertain:
                # Fallback auf Standard-Split
                mb_artist, mb_title, mb_album, mb_album_date, mbid, mb_first_release, uncertain, duration_ms = _identify_artist_title_via_musicbrainz(artist, title)
        else:
            mb_artist, mb_title, mb_album, mb_album_date, mbid, mb_first_release, uncertain, duration_ms = _identify_artist_title_via_musicbrainz(artist, title)

        if uncertain:
            # ICY-Standard beibehalten
            mb_artist, mb_title = artist or None, title or None
            mb_album, mb_album_date, mbid, mb_first_release, duration_ms = '', '', '', '', 0
            
        if mb_artist in invalid: mb_artist = None
        if mb_title in invalid:  mb_title  = None
        if not mb_artist and not mb_title:
            self._set_last_song_decision('', None, None)
            return None, None, '', '', '', '', 0

        self._set_last_song_decision('icy', mb_artist, mb_title)
        if self.mp_decision_enabled:
            self._log_musicplayer_comparison('icy', (mb_artist, mb_title), mp_pairs)
            self._update_musicplayer_trust_after_decision('icy', (mb_artist, mb_title), mp_pairs)
        return mb_artist, mb_title, mb_album, mb_album_date, mbid, mb_first_release, duration_ms
        
    def metadata_worker(self, url, generation):
        """Worker-Thread zum kontinuierlichen Auslesen der Metadaten"""
        log_debug(f"Metadata Worker gestartet")

        # Timer-Status beim Start des Workers sauber initialisieren.
        # Gilt auch für den No-ICY-Pfad (API/MusicPlayer-Fallback).
        self._reset_song_timeout_state(clear_debug=True)
        self.startup_qualifier.reset_session()

        stream_info = self.parse_icy_metadata(url)
        if not stream_info:
            xbmc.log(f"[{ADDON_NAME}] Keine ICY-Metadaten verfuegbar - wechsle zu Fallback", xbmc.LOGWARNING)
            WINDOW.clearProperty(_P.ICY_NOW)
            if not self._is_api_source_allowed():
                self._log_api_source_blocked('metadata_worker_no_icy')
            if (
                self._is_api_source_allowed()
                and (self.use_api_fallback or self.plugin_slug or self.tunein_station_id)
                and generation == self.metadata_generation
            ):
                self.api_metadata_worker(generation)
            elif generation == self.metadata_generation and self.mp_decision_enabled:
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

                        station_name = stream_info.get('station', '')
                        invalid_values = INVALID_METADATA_VALUES + ["", station_name]
                        current_mp_pair = ('', '')
                        if self.mp_decision_enabled:
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

                        # API-Daten erst nach stabilem Start oder nach gesetzter Erstquelle aktualisieren.
                        # Dadurch wird waehrend sichtbarem Kodi-Buffering kein API-Property vorbefuellt.
                        api_refresh_allowed = startup_stable_confirmed or bool(last_winner_source)
                        if api_refresh_allowed:
                            self._refresh_api_nowplaying_property(station_name)
                            current_api_pair = self._pair_for_source(last_winner_source, self._latest_api_pair)
                        else:
                            self._latest_api_pair = ('', '')
                            current_api_pair = ('', '')

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
                                if self.mp_decision_enabled:
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
                                self._refresh_api_nowplaying_property(station_name)
                                current_api_pair = self._pair_for_source(last_winner_source, self._latest_api_pair)
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
                        source_changed_trigger, trigger_reason = self._determine_source_change_trigger(
                            last_winner_source,
                            last_winner_pair,
                            current_mp_pair,
                            current_api_pair,
                            current_icy_pair,
                            station_name,
                            stream_title_changed,
                            (initial_source_pending if allow_initial_trigger else False)
                        )

                        mp_generic_hold_active = False
                        if self.mp_decision_enabled:
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
                            if not self.mp_decision_enabled:
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
                            if self.mp_decision_enabled and trigger_reason.startswith('MusicPlayer'):
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
                            xbmc.log(f"[{ADDON_NAME}] ICY-Daten: station='{station_name}', stream_title='{stream_title}'", xbmc.LOGINFO)

                            # Artist und Title trennen – API wird intern in parse_stream_title aufgerufen
                            parse_locked_source = last_winner_source
                            if self.mp_decision_enabled and trigger_reason == self.TRIGGER_MP_CHANGE:
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
                            if self.mp_decision_enabled:
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
                                continue
                            
                            if stream_title not in INVALID_METADATA_VALUES:
                                self.set_property_safe(_P.STREAM_TTL, stream_title)
                                self._set_icy_nowplaying_label(stream_title=stream_title)
                            
                            # Reihenfolge: Title und MBID vor Artist setzen.
                            # AS lauscht auf RadioMonitor.Artist als Trigger und liest
                            # danach sofort RadioMonitor.MBID – daher muss MBID bereits
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
                                log_debug(f"Artist: {artist}")
                            else:
                                WINDOW.clearProperty(_P.ARTIST)
                                artist = ''

                            # Logo sofort nach Artist setzen – vor dem optionalen Artist-Info-Call,
                            # damit der time.sleep(1) das Logo nicht verzögert.
                            self.set_logo_safe()

                            # Song-Timeout: Timer (neu) starten sobald ein Titel erkannt wurde.
                            # Bei MB-Laenge: Laenge - SONG_TIMEOUT_EARLY_CLEAR_S.
                            # Ohne MB-Laenge greift SONG_TIMEOUT_FALLBACK_S.
                            if title and is_new_song:
                                self._start_song_timeout(duration_ms)

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
                            log_debug(f"RadioMonitor.MBID = {WINDOW.getProperty(_P.MBID)}")
                            log_debug(f"RadioMonitor.FirstRelease = {WINDOW.getProperty(_P.FIRST_REL)}")
                            log_debug(f"RadioMonitor.BandFormed = {WINDOW.getProperty(_P.BAND_FORM)}")
                            log_debug(f"RadioMonitor.BandMembers = {WINDOW.getProperty(_P.BAND_MEM)}")
                            log_debug(f"RadioMonitor.StreamTitle = {WINDOW.getProperty(_P.STREAM_TTL)}")
                            log_debug(f"RadioMonitor.Genre = {WINDOW.getProperty(_P.GENRE)}")
                            log_debug(f"RadioMonitor.Logo = {WINDOW.getProperty(_P.LOGO)}")

                            # Aktualisiere Kodi Player Metadaten (fuer Standard InfoLabels)
                            winner_source_for_player = str(decision_source or last_winner_source or '')
                            if winner_source_for_player.startswith('musicplayer'):
                                log_debug("Player InfoTag Update uebersprungen (Quelle=musicplayer)")
                            else:
                                logo = WINDOW.getProperty(_P.LOGO)
                                self.update_player_metadata(artist if artist else None,
                                                            title if title else None,
                                                            album if album else station_name,
                                                            logo if logo else None,
                                                            mbid if mbid else None)
                            
                            self._capture_playing_item_raw()
                            self._capture_jsonrpc_player_raw()
                            
                            log_debug("========================")
                            
                            # Versuche die MusicPlayer InfoLabels zu überschreiben
                            # indem wir die JSON-RPC API nutzen
                            try:
                                json_query = {
                                    "jsonrpc": "2.0",
                                    "method": "JSONRPC.NotifyAll",
                                    "params": {
                                        "sender": "service.audio.stream.monitor",
                                        "message": "UpdateMusicInfo",
                                        "data": {
                                            "artist": artist,
                                            "title": title,
                                            "streamtitle": stream_title,
                                            "mbid": mbid if mbid else ""
                                        }
                                    },
                                    "id": 1
                                }
                                xbmc.executeJSONRPC(json.dumps(json_query))
                            except Exception as e:
                                log_debug(f"Fehler bei JSON-RPC Notify: {str(e)}")
                            
                            xbmc.log(f"[{ADDON_NAME}] Neuer Titel: {stream_title} (Artist: {artist if artist else 'N/A'}, Title: {title if title else 'N/A'}, Album: {album if album else 'N/A'})", xbmc.LOGINFO)
                            if title:
                                last_song_key = current_song_key
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
                        elif stream_title_changed and last_winner_source:
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
                        self._refresh_api_nowplaying_property(stream_info.get('station', ''))
                    self._update_station_profile(stream_info.get('station', ''))
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
                            tunein_id = self._extract_tunein_station_id(playing_file)
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
                            
                            # 2. Fallback: Window-Property vom radio.de Addon
                            if not logo:
                                radiode_logo = WINDOW.getProperty(_P.RADIODE_LOGO)
                                if self.is_real_logo(radiode_logo):
                                    logo = radiode_logo
                                    self.station_logo = logo
                                    self._ensure_api_source_from_context(logo, 'check_playing_radiode_logo')
                            
                            # 3. Fallback: Player Art
                            if not logo:
                                for source, player_logo in player_art_candidates.items():
                                    if self.is_real_logo(player_logo):
                                        logo = player_logo
                                        self.station_logo = logo
                                        self._ensure_api_source_from_context(logo, f'check_playing_{source}')
                                        break

                            if not self.station_logo or not self.is_real_logo(self.station_logo):
                                log_debug("Kein Player-Logo, wird spaeter von API geholt")
                            
                            # Setze Logo (nur wenn echtes Logo, sonst Kodi-Fallback)
                            self.set_logo_safe()
                            if self.station_logo and self.is_real_logo(self.station_logo):
                                xbmc.log(f"[{ADDON_NAME}] Logo gesetzt: {self.station_logo}", xbmc.LOGINFO)
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
                                        xbmc.log(f"[{ADDON_NAME}] Station-Logo gefunden: {logo_url}", xbmc.LOGINFO)
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
                        
                        xbmc.log(f"[{ADDON_NAME}] ========================================", xbmc.LOGINFO)
                        
                        # ICY-Metadaten-Monitoring starten
                        self.start_metadata_monitoring(playing_file)

                        xbmc.log(f"[{ADDON_NAME}] Stream erkannt: {playing_file}", xbmc.LOGINFO)
                else:
                    # Kein Stream - Properties löschen
                    if self.is_playing:
                        self.is_playing = False
                        self.current_url = None
                        self.stop_metadata_monitoring()
                        self.clear_properties()
            except Exception as e:
                xbmc.log(f"[{ADDON_NAME}] Fehler beim Überprüfen des Players: {str(e)}", xbmc.LOGERROR)
        else:
            # Nichts wird abgespielt
            if self.is_playing:
                self.is_playing = False
                self.current_url = None
                self.stop_metadata_monitoring()
                self.clear_properties()
                xbmc.log(f"[{ADDON_NAME}] Wiedergabe gestoppt", xbmc.LOGINFO)
                
    def run(self):
        """Haupt-Loop des Services"""
        # Initial properties löschen
        self.clear_properties()
        
        # Haupt-Loop
        while not self.abortRequested():
            # Alle 2 Sekunden überprüfen
            if self.waitForAbort(2):
                break
                
            self.check_playing()
            self._update_timeout_remaining_property()
            
        # Cleanup beim Beenden
        self.stop_metadata_monitoring()
        self._flush_station_profiles()
        try:
            if self.analysis_store is not None:
                self.analysis_store.close()
        except Exception as e:
            log_debug(f"Analysis store close fehlgeschlagen: {e}")
        self.api_client.close()
        self.clear_properties()
        xbmc.log(f"[{ADDON_NAME}] Service beendet", xbmc.LOGINFO)

if __name__ == '__main__':
    monitor = RadioMonitor()
    monitor.run()
