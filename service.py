import xbmc
import xbmcgui
import requests
import re
import time
import threading
import json
from urllib.parse import urlparse, parse_qs, unquote

# --- Modul-Imports ---
from constants import (
    ADDON, ADDON_ID, ADDON_NAME, ADDON_VERSION,
    RADIODE_SEARCH_API_URL, RADIODE_NOWPLAYING_API_URL, RADIODE_DETAILS_API_URL,
    TUNEIN_DESCRIBE_API_URL, TUNEIN_TUNE_API_URL,
    DEFAULT_HTTP_HEADERS, INVALID_METADATA_VALUES,
    SONG_TIMEOUT_FALLBACK_S, SONG_TIMEOUT_EARLY_CLEAR_S,
    API_DATA_REFRESH_INTERVAL_S, PLAYER_BUFFER_SETTLE_S, PLAYER_BUFFER_MAX_WAIT_S,
    PropertyNames as _P, NUMERIC_ID_PATTERN as _NUMERIC_ID_RE
)
from logger import log_debug, log_info, log_warning, log_error
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
                        xbmc.log(f"[{ADDON_NAME}] Plugin-Slug aus iconimage: '{slug}'", xbmc.LOGINFO)
            elif 'plugin.audio.radiode' in playing_file:
                self.radio_monitor._set_api_source(self.radio_monitor.API_SOURCE_RADIODE)
                xbmc.log(f"[{ADDON_NAME}] radio.de Addon erkannt (plugin.audio.radiode)", xbmc.LOGDEBUG)
            elif 'plugin.audio.tunein2017' in playing_file:
                self.radio_monitor._set_api_source(self.radio_monitor.API_SOURCE_TUNEIN)
                tunein_id = self.radio_monitor._extract_tunein_station_id(playing_file)
                if tunein_id:
                    self.radio_monitor.tunein_station_id = tunein_id
                    xbmc.log(f"[{ADDON_NAME}] TuneIn Station-ID aus Plugin-URL: '{tunein_id}'", xbmc.LOGINFO)

            # Fallback: Addon-Plugin-ID ist nicht immer im aufgeloesten PlayingFile enthalten.
            # Dann Quelle aus URL-Hints (z.B. aggregator=tunein / aggregator=radio-de) ableiten.
            self.radio_monitor._ensure_api_source_from_context(playing_file, 'onPlayBackStarted')
        except Exception as e:
            xbmc.log(f"[{ADDON_NAME}] Fehler in onPlayBackStarted: {e}", xbmc.LOGDEBUG)

    def onPlayBackStopped(self):
        try:
            self.radio_monitor._handle_playback_stop("onPlayBackStopped")
        except Exception as e:
            xbmc.log(f"[{ADDON_NAME}] Fehler in onPlayBackStopped: {e}", xbmc.LOGDEBUG)

    def onPlayBackEnded(self):
        try:
            self.radio_monitor._handle_playback_stop("onPlayBackEnded")
        except Exception as e:
            xbmc.log(f"[{ADDON_NAME}] Fehler in onPlayBackEnded: {e}", xbmc.LOGDEBUG)

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
                    xbmc.log(f"[{ADDON_NAME}] ⚡ Logo SOFORT beim Start erfasst: {listitem_icon}", xbmc.LOGINFO)
                else:
                    xbmc.log(f"[{ADDON_NAME}] ⚠ ListItem.Icon beim Start: {listitem_icon}", xbmc.LOGDEBUG)
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
    RADIODE_PLUGIN_IDS = ('plugin.audio.radiode', 'plugin.audio.radio_de_light')
    TUNEIN_PLUGIN_IDS = ('plugin.audio.tunein2017',)
    RADIODE_URL_HINTS = ('radio.de', 'radio-assets.com')
    TUNEIN_URL_HINTS = ('tunein.com', 'radiotime.com', 'cdn-profiles.tunein.com')
    MB_WINNER_MIN_SCORE = 60
    MB_WINNER_MIN_COMBINED = 55.0
    MP_TRUST_MAX_MISMATCHES = 2
    TRIGGER_TITLE_CHANGE = 'Titelwechsel'
    TRIGGER_API_CHANGE = 'API-Wechsel'
    TRIGGER_MP_CHANGE = 'MusicPlayer-Wechsel'
    TRIGGER_MP_INVALID = 'MusicPlayer ungueltig'

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
        self._last_api_data_refresh_ts = 0.0
        self._mp_trusted = False
        self._mp_mismatch_count = 0
        self._mp_trust_generation = 0
        self._latest_api_pair = ('', '')
        self._last_decision_source = ''
        self._last_decision_pair = ('', '')
        self._parse_prev_winner_pair = ('', '')
        self._parse_trigger_reason = ''
        self._parse_locked_source = ''
        
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
                f"(context={context}, source={inferred})",
                xbmc.LOGDEBUG
            )
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
        xbmc.log(
            f"[{ADDON_NAME}] API uebersprungen: Source nicht whitelisted (context={context}, source={source})",
            xbmc.LOGDEBUG
        )

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
        was_trusted = self._mp_trusted
        self._mp_trusted = False
        self._mp_mismatch_count = 0
        self._mp_trust_generation = self.metadata_generation
        if reason and was_trusted:
            xbmc.log(f"[{ADDON_NAME}] MusicPlayer-Trust zurueckgesetzt: {reason}", xbmc.LOGDEBUG)

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
            xbmc.log(
                f"[{ADDON_NAME}] Song-Timeout: {self._song_timeout:.0f}s "
                f"(MB-Laenge: {mb_duration_ms}ms, -{SONG_TIMEOUT_EARLY_CLEAR_S}s, fallback={SONG_TIMEOUT_FALLBACK_S}s)",
                xbmc.LOGDEBUG
            )
        else:
            xbmc.log(
                f"[{ADDON_NAME}] Song-Timeout: {self._song_timeout:.0f}s "
                f"(MB-Laenge: unbekannt, fallback={SONG_TIMEOUT_FALLBACK_S}s)",
                xbmc.LOGDEBUG
            )

    def clear_properties(self):
        """Löscht alle Radio-Properties"""
        # Reset Logo und API-Kontext
        self.station_logo = None
        self._reset_api_context()
        self._reset_song_timeout_state(clear_debug=True)
        self._reset_musicplayer_trust_state('clear_properties')
        self._api_timeout_block_key = ('', '')
        self._last_seen_api_key = ('', '')
        self._last_api_data_refresh_ts = 0.0
        self._latest_api_pair = ('', '')
        self._last_decision_source = ''
        self._last_decision_pair = ('', '')
        self._parse_prev_winner_pair = ('', '')
        self._parse_trigger_reason = ''
        self._parse_locked_source = ''

        # Lösche auch radio.de Addon Properties
        WINDOW.clearProperty('RadioDE.StationLogo')
        WINDOW.clearProperty('RadioDE.StationName')
        
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
        WINDOW.clearProperty(_P.API_DATA)
        WINDOW.clearProperty(_P.SOURCE)
        WINDOW.clearProperty(_P.PLAYING)
        WINDOW.clearProperty(_P.LOGO)
        WINDOW.clearProperty(_P.BAND_FORM)
        WINDOW.clearProperty(_P.BAND_MEM)
        
        xbmc.log(f"[{ADDON_NAME}] Properties gelöscht", xbmc.LOGDEBUG)
        
    def _handle_stream_transition(self, reason=''):
        """
        Streamwechsel: alte Labels sofort leeren, bevor neue Daten gesetzt werden.
        """
        self.stop_metadata_monitoring()
        self.is_playing = False
        self.current_url = None
        self.clear_properties()
        if reason:
            xbmc.log(f"[{ADDON_NAME}] Streamwechsel: Labels geleert ({reason})", xbmc.LOGDEBUG)

    def _handle_playback_stop(self, reason=''):
        """
        Playback-Stop/Ende: Labels sofort leeren (ohne 2s Poll-Wartezeit).
        """
        self.stop_metadata_monitoring()
        self.is_playing = False
        self.current_url = None
        self.clear_properties()
        if reason:
            xbmc.log(f"[{ADDON_NAME}] Wiedergabe beendet: Labels sofort geleert ({reason})", xbmc.LOGDEBUG)

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
                            xbmc.log(f"[{ADDON_NAME}] Player MBID gesetzt über {method_name}: {mbid}", xbmc.LOGDEBUG)
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
                xbmc.log(f"[{ADDON_NAME}] Player InfoTag aktualisiert: {artist} - {title}", xbmc.LOGDEBUG)
            except AttributeError:
                # Fallback: Setze Properties, die Skins nutzen können
                xbmc.log(f"[{ADDON_NAME}] updateInfoTag() nicht verfügbar - nutze nur Window Properties", xbmc.LOGDEBUG)
            
        except Exception as e:
            xbmc.log(f"[{ADDON_NAME}] Fehler beim Aktualisieren der Player Metadaten: {str(e)}", xbmc.LOGDEBUG)
            
    def _setup_api_fallback_from_url(self, url):
        """
        Versucht den Stationsnamen aus der Stream-URL zu extrahieren und setzt
        das API-Fallback-Flag, wenn kein icy-metaint Header verfügbar ist.
        Wird aufgerufen wenn der Stream keine ICY-Metadaten liefert.
        """
        if not self._is_api_source_allowed():
            self._log_api_source_blocked('setup_api_fallback_from_url')
            return None

        try:
            if self._can_use_radiode_api():
                self.use_api_fallback = True
                xbmc.log(f"[{ADDON_NAME}] radio.de Stream erkannt, versuche Stationsnamen aus URL", xbmc.LOGDEBUG)

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
                    xbmc.log(f"[{ADDON_NAME}] Station aus URL erkannt: {station_name}", xbmc.LOGDEBUG)

                    self.station_slug = station_slug

                    return station_name

            if self._can_use_tunein_api():
                self.use_api_fallback = True
                tunein_id = self._extract_tunein_station_id(url)
                if tunein_id:
                    self.tunein_station_id = tunein_id
                    xbmc.log(f"[{ADDON_NAME}] TuneIn Stream erkannt, Station-ID aus URL: '{tunein_id}'", xbmc.LOGDEBUG)
                else:
                    xbmc.log(f"[{ADDON_NAME}] TuneIn Stream erkannt, aber keine Station-ID in URL gefunden", xbmc.LOGDEBUG)
        except Exception as e:
            xbmc.log(f"[{ADDON_NAME}] Fehler bei URL-Analyse fuer API-Fallback: {str(e)}", xbmc.LOGDEBUG)
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
                response = requests.get(endpoint, params=params, headers=DEFAULT_HTTP_HEADERS, timeout=5)
                if response.status_code != 200:
                    xbmc.log(
                        f"[{ADDON_NAME}] TuneIn API Status {response.status_code} für {endpoint}",
                        xbmc.LOGDEBUG
                    )
                    continue

                try:
                    payload = response.json()
                except Exception:
                    payload = None

                if payload is not None:
                    artist, title = self._extract_tunein_from_json(payload, station_name)
                    if artist or title:
                        xbmc.log(f"[{ADDON_NAME}] ✓ TuneIn API: {artist} - {title}", xbmc.LOGINFO)
                        return artist, title

                artist, title = self._extract_tunein_from_text(response.text, station_name)
                if artist or title:
                    xbmc.log(f"[{ADDON_NAME}] ✓ TuneIn API (Text): {artist} - {title}", xbmc.LOGINFO)
                    return artist, title
            except Exception as e:
                xbmc.log(f"[{ADDON_NAME}] Fehler bei TuneIn API Abfrage ({endpoint}): {e}", xbmc.LOGDEBUG)

        return None, None

    def _refresh_api_data_property(self, station_name=None, force=False):
        """
        Aktualisiert RadioMonitor.ApiData periodisch aus der aktiven API-Quelle.
        Nutzt bewusst keinen MusicPlayer-Fallback, damit das Label nur echte API-Daten zeigt.
        """
        if not self._is_api_source_allowed():
            WINDOW.clearProperty(_P.API_DATA)
            self._latest_api_pair = ('', '')
            return

        now_ts = time.time()
        if not force and (now_ts - self._last_api_data_refresh_ts) < API_DATA_REFRESH_INTERVAL_S:
            return
        self._last_api_data_refresh_ts = now_ts

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
                self.set_property_safe(_P.API_DATA, f"{n_artist} - {n_title}")
            else:
                self._latest_api_pair = ('', '')
                self.set_property_safe(_P.API_DATA, f"{artist} - {title}" if artist else title)
        else:
            WINDOW.clearProperty(_P.API_DATA)
            self._latest_api_pair = ('', '')
    
    def get_nowplaying_from_apis(self, station_name, stream_url):
        """Versucht nowPlaying von verschiedenen APIs zu holen"""
        if not self._is_api_source_allowed():
            self._log_api_source_blocked('get_nowplaying_from_apis')
            WINDOW.clearProperty(_P.API_DATA)
            return None, None

        xbmc.log(f"[{ADDON_NAME}] API-Fallback gestartet für Station: '{station_name}'", xbmc.LOGDEBUG)

        # 1. radio.de API nur wenn Source=radio.de
        if self._can_use_radiode_api() and (station_name or self.plugin_slug):
            artist, title = self.get_radiode_api_nowplaying(station_name)
            if artist or title:
                xbmc.log(f"[{ADDON_NAME}] ✓ radio.de API: {artist} - {title}", xbmc.LOGINFO)
                self.set_property_safe(_P.API_DATA, f"{artist} - {title}" if artist else title)
                return artist, title

        # 2. TuneIn API nur wenn Source=TuneIn
        if self._can_use_tunein_api():
            artist, title = self.get_tunein_api_nowplaying(station_name)
            if artist or title:
                xbmc.log(f"[{ADDON_NAME}] ✓ TuneIn API: {artist} - {title}", xbmc.LOGINFO)
                self.set_property_safe(_P.API_DATA, f"{artist} - {title}" if artist else title)
                return artist, title

        # 3. Fallback: Kodi Player InfoTags
        WINDOW.clearProperty(_P.API_DATA)
        try:
            if self.player.isPlayingAudio():
                info_tag = self.player.getMusicInfoTag()
                title = info_tag.getTitle()
                artist = info_tag.getArtist()
                
                invalid_values = INVALID_METADATA_VALUES + ['', station_name]
                if title and title not in invalid_values:
                    # Filter Zahlen-IDs
                    if _NUMERIC_ID_RE.match(title):
                        xbmc.log(f"[{ADDON_NAME}] Player InfoTag enthält Zahlen-ID, ignoriere: {title}", xbmc.LOGDEBUG)
                        return None, None
                    
                    # Filter einzelne Zahlen als Artist
                    if artist and re.match(r'^\d+$', artist):
                        xbmc.log(f"[{ADDON_NAME}] Player InfoTag Artist ist nur eine Zahl, ignoriere: {artist}", xbmc.LOGDEBUG)
                        artist = None
                    
                    # Filter einzelne Zahlen als Title
                    if title and re.match(r'^\d+$', title):
                        xbmc.log(f"[{ADDON_NAME}] Player InfoTag Title ist nur eine Zahl, ignoriere: {title}", xbmc.LOGDEBUG)
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
            xbmc.log(f"[{ADDON_NAME}] Fehler beim Lesen Player InfoTags: {str(e)}", xbmc.LOGDEBUG)
        
        WINDOW.clearProperty(_P.API_DATA)
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
                            f"'{label_pair[0]} - {label_pair[1]}'",
                            xbmc.LOGDEBUG
                        )
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
            xbmc.log(f"[{ADDON_NAME}] Fehler beim Lesen MusicPlayer Kandidat: {str(e)}", xbmc.LOGDEBUG)
        return mp_direct, mp_swapped

    def _valid_song_pairs(self, *pairs):
        """Filtert nur valide (artist, title)-Paare."""
        return [p for p in pairs if p and p[0] and p[1]]

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
                    xbmc.log(
                        f"[{ADDON_NAME}] Quellenfestlegung ausgesetzt: Player puffert noch",
                        xbmc.LOGDEBUG
                    )
                    logged_buffering = True
                logged_settle_wait = False
            else:
                if stable_since is None:
                    stable_since = time.time()
                    if not logged_settle_wait:
                        xbmc.log(
                            f"[{ADDON_NAME}] Quellenfestlegung wartet auf stabilen Start "
                            f"({PLAYER_BUFFER_SETTLE_S:.1f}s ohne Buffering)",
                            xbmc.LOGDEBUG
                        )
                        logged_settle_wait = True
                    logged_buffering = False
                elif (time.time() - stable_since) >= PLAYER_BUFFER_SETTLE_S:
                    return True

            if (time.time() - wait_started) >= PLAYER_BUFFER_MAX_WAIT_S:
                xbmc.log(
                    f"[{ADDON_NAME}] Buffering-Check Timeout nach {PLAYER_BUFFER_MAX_WAIT_S:.0f}s - "
                    f"setze Quellenfestlegung fort",
                    xbmc.LOGWARNING
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
        stream_title_changed,
        initial_source_pending
    ):
        """
        Zentrale Trigger-Erkennung.
        Wichtig: Nach der Erstfestlegung wird nur die aktive Quelle bewertet.
        """
        if not last_winner_source:
            return (stream_title_changed or initial_source_pending), self.TRIGGER_TITLE_CHANGE

        if last_winner_source.startswith('icy'):
            return stream_title_changed, self.TRIGGER_TITLE_CHANGE

        if last_winner_source.startswith('api'):
            changed = (
                current_api_pair[0]
                and current_api_pair[1]
                and current_api_pair != last_winner_pair
            )
            return changed, self.TRIGGER_API_CHANGE

        if last_winner_source.startswith('musicplayer'):
            changed = (
                current_mp_pair[0]
                and current_mp_pair[1]
                and current_mp_pair != last_winner_pair
            )
            if changed:
                return True, self.TRIGGER_MP_CHANGE
            invalid = bool(last_winner_pair[0] and not (current_mp_pair[0] and current_mp_pair[1]))
            if invalid:
                return True, self.TRIGGER_MP_INVALID
            return False, ''

        return False, ''

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
                f"[{ADDON_NAME}] Source-Lock geloest: '{locked_family}' ohne valide Daten -> Fallback aktiv",
                xbmc.LOGDEBUG
            )
            return candidates

        locked_candidates = [
            c for c in candidates
            if self._source_family(c.get('source')) == locked_family
        ]
        if locked_candidates:
            xbmc.log(
                f"[{ADDON_NAME}] Source-Lock aktiv: '{locked_family}' "
                f"(candidates={len(locked_candidates)})",
                xbmc.LOGDEBUG
            )
            return locked_candidates
        return candidates

    def _resolve_mb_zero_with_source_lock(self, locked_source, mp_pairs, api_candidate, icy_pairs):
        """
        MB=0 Fallback fuer source-locked Auswertungen:
        - Bei aktivem Lock darf keine andere Quelle den Lock uebersteuern.
        Rueckgabe: (source_family, (artist, title)) oder ('', ('', '')).
        """
        locked_family = self._source_family(locked_source)
        if locked_family == 'musicplayer' and mp_pairs:
            return 'musicplayer', mp_pairs[0]
        if (
            locked_family == 'api'
            and api_candidate
            and api_candidate[0]
            and api_candidate[1]
        ):
            return 'api', api_candidate
        if locked_family == 'icy' and icy_pairs:
            return 'icy', icy_pairs[0]
        return '', ('', '')

    def _apply_api_stale_override(self, candidates, trigger_reason, api_candidate, icy_pairs):
        """
        Spezialfall fuer API-stale Trigger:
        - Wenn ICY einen Kandidaten hat und Trigger='MusicPlayer-Wechsel (API stale)',
          werden API-Kandidaten ausgeblendet.
        """
        if trigger_reason != 'MusicPlayer-Wechsel (API stale)' or not icy_pairs:
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
            xbmc.log(
                f"[{ADDON_NAME}] API-stale Override aktiv: {removed} Kandidaten ausgeblendet "
                f"(trigger={trigger_reason})",
                xbmc.LOGDEBUG
            )
            return filtered
        return candidates

    def _set_last_song_decision(self, source, artist=None, title=None):
        """Speichert die letzte Gewinnerquelle zentral fuer source-locked Trigger."""
        self._last_decision_source = str(source or '')
        source_family = self._source_family(self._last_decision_source)
        if source_family in ('musicplayer', 'icy', 'api'):
            self.set_property_safe(_P.SOURCE, source_family)
        else:
            WINDOW.clearProperty(_P.SOURCE)
        if artist and title:
            self._last_decision_pair = (artist, title)
        else:
            self._last_decision_pair = ('', '')

    def _is_musicplayer_trusted(self):
        """MusicPlayer ist nur innerhalb der aktuellen Metadata-Generation vertrauenswürdig."""
        return self._mp_trusted and self._mp_trust_generation == self.metadata_generation

    def _mark_musicplayer_trusted(self, reason=''):
        """Promotet MusicPlayer als vertrauenswürdige Songquelle."""
        was_trusted = self._is_musicplayer_trusted()
        self._mp_trusted = True
        self._mp_trust_generation = self.metadata_generation
        self._mp_mismatch_count = 0
        if not was_trusted:
            suffix = f" ({reason})" if reason else ""
            xbmc.log(f"[{ADDON_NAME}] MusicPlayer als Songquelle verifiziert{suffix}", xbmc.LOGINFO)

    def _register_musicplayer_mismatch(self, reason=''):
        """Registriert einen Trust-Fehler (z. B. fehlende/ungueltige MusicPlayer-Daten)."""
        if not self._is_musicplayer_trusted():
            return
        self._mp_mismatch_count += 1
        xbmc.log(
            f"[{ADDON_NAME}] MusicPlayer-Widerspruch ({self._mp_mismatch_count}/{self.MP_TRUST_MAX_MISMATCHES})"
            f"{': ' + reason if reason else ''}",
            xbmc.LOGDEBUG
        )
        if self._mp_mismatch_count >= self.MP_TRUST_MAX_MISMATCHES:
            self._reset_musicplayer_trust_state('zu viele Widersprueche')

    def _update_musicplayer_trust_after_decision(self, decision_source, decision_pair, mp_pairs):
        """
        Aktualisiert den MusicPlayer-Trust nach einer finalen Song-Entscheidung.
        Regel:
        - Trust wird nur dann aufgebaut, wenn eine externe Entscheidung (api/icy) den MP bestätigt.
        - Im MP-master Modus wird bei externem Widerspruch nicht automatisch de-vertraut.
        """
        if not mp_pairs:
            return
        source = str(decision_source or '')
        pair = decision_pair if decision_pair and decision_pair[0] and decision_pair[1] else None
        mp_set = set(mp_pairs)

        if source.startswith('musicplayer'):
            if self._is_musicplayer_trusted():
                self._mp_mismatch_count = 0
            return

        if source.startswith('api') or source.startswith('icy'):
            if pair and pair in mp_set:
                self._mark_musicplayer_trusted(f"Konsens mit {source}: '{pair[0]} - {pair[1]}'")

    def _should_use_musicplayer_candidates(self, mp_pairs, api_candidate, icy_pairs):
        """
        Steuert zentral, ob MusicPlayer-Kandidaten in die MB-Wahl aufgenommen werden.
        Policy:
        - Wenn MusicPlayer valide (artist+title) liefert, wird er immer mitbewertet.
        - Die eigentliche Gueltigkeit wird danach ueber die MB-Schwellen geprueft.
        """
        _ = api_candidate
        _ = icy_pairs
        return bool(mp_pairs)

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
            xbmc.log(
                f"[{ADDON_NAME}] MB-Kandidat[{ev['source']}]: "
                f"in='{ev['input_artist']} - {ev['input_title']}', "
                f"score={ev['score']}, artist_sim={ev['artist_sim']:.2f}, "
                f"title_sim={ev['title_sim']:.2f}, combined={ev['combined']:.1f}",
                xbmc.LOGDEBUG
            )

        valid = [
            ev for ev in evaluations
            if ev['score'] >= self.MB_WINNER_MIN_SCORE and ev['combined'] >= self.MB_WINNER_MIN_COMBINED
        ]
        if not valid:
            xbmc.log(
                f"[{ADDON_NAME}] MB-Winner: kein Kandidat über Schwellwert "
                f"(min_score={self.MB_WINNER_MIN_SCORE}, min_combined={self.MB_WINNER_MIN_COMBINED:.1f})",
                xbmc.LOGDEBUG
            )
            return None, evaluations

        def _source_rank(source):
            s = str(source or '')
            if s.startswith('musicplayer'):
                return 2
            if s.startswith('icy'):
                return 1
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
                xbmc.log(
                    f"[{ADDON_NAME}] MB-Winner: Vorheriger Winner geblockt "
                    f"(Trigger={trigger_reason}, entfernt={filtered_count})",
                    xbmc.LOGDEBUG
                )

        winner_pool = effective_valid
        musicplayer_pool = [
            ev for ev in effective_valid
            if str(ev.get('source', '')).startswith('musicplayer')
        ]
        all_mp_match_prev = bool(musicplayer_pool) and prev_pair_valid and all(
            (ev.get('input_artist'), ev.get('input_title')) == prev_pair
            for ev in musicplayer_pool
        )
        stale_mp_hold = (
            bool(trigger_reason)
            and not trigger_reason.startswith('MusicPlayer')
            and all_mp_match_prev
        )

        if musicplayer_pool and not stale_mp_hold:
            winner_pool = musicplayer_pool
            xbmc.log(
                f"[{ADDON_NAME}] MB-Winner: MusicPlayer-Praeferenz aktiv "
                f"(candidates={len(musicplayer_pool)})",
                xbmc.LOGDEBUG
            )
        else:
            if stale_mp_hold:
                xbmc.log(
                    f"[{ADDON_NAME}] MB-Winner: MusicPlayer-Praeferenz ausgesetzt "
                    f"(Trigger={trigger_reason}, MP entspricht vorherigem Winner)",
                    xbmc.LOGDEBUG
                )
            # Fallback ohne MP-Praeferenz:
            # Wenn mindestens zwei unabhaengige Quellenfamilien (API/ICY/MusicPlayer)
            # dasselbe Eingangspaar liefern, wird der Winner auf diese Konsenspaare
            # eingeschraenkt.
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
                    xbmc.log(
                        f"[{ADDON_NAME}] MB-Winner: Konsens aktiv "
                        f"(pairs={len(consensus_pairs)}, candidates={len(consensus_pool)})",
                        xbmc.LOGDEBUG
                    )

        winner = max(
            winner_pool,
            key=lambda ev: (
                ev['combined'],
                ev['score'],
                _source_rank(ev.get('source'))
            )
        )
        xbmc.log(
            f"[{ADDON_NAME}] MB-Winner: source={winner['source']} "
            f"('{winner['mb_artist']} - {winner['mb_title']}'), "
            f"score={winner['score']}, combined={winner['combined']:.1f}",
            xbmc.LOGINFO
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
                xbmc.log(f"[{ADDON_NAME}] Station-Slug: '{slug}' (plugin={bool(self.plugin_slug)})", xbmc.LOGDEBUG)
                try:
                    det_response = requests.get(RADIODE_DETAILS_API_URL, params={'stationIds': slug}, headers=DEFAULT_HTTP_HEADERS, timeout=5)
                    xbmc.log(f"[{ADDON_NAME}] Details-API Status: {det_response.status_code}, URL: {det_response.url}", xbmc.LOGDEBUG)
                    if det_response.status_code == 200:
                        det_data = det_response.json()
                        xbmc.log(f"[{ADDON_NAME}] Details-API Response: {str(det_data)[:300]}", xbmc.LOGDEBUG)
                        if isinstance(det_data, list) and len(det_data) > 0:
                            proper_name = det_data[0].get('name', '')
                            if proper_name:
                                self.set_property_safe(_P.STATION, proper_name)
                                xbmc.log(f"[{ADDON_NAME}] Station aus Details-API: '{proper_name}'", xbmc.LOGINFO)
                            det_logo = det_data[0].get('logo300x300', '')
                            if det_logo and not self.station_logo:
                                self.station_logo = det_logo
                                self.set_logo_safe()
                                xbmc.log(f"[{ADDON_NAME}] Logo aus Details-API: '{det_logo}'", xbmc.LOGINFO)
                except Exception as e:
                    xbmc.log(f"[{ADDON_NAME}] Fehler bei Details-API: {e}", xbmc.LOGDEBUG)

                try:
                    np_response = requests.get(RADIODE_NOWPLAYING_API_URL, params={'stationIds': slug}, headers=DEFAULT_HTTP_HEADERS, timeout=5)
                    if np_response.status_code == 200:
                        np_data = np_response.json()
                        xbmc.log(f"[{ADDON_NAME}] now-playing API Response (Slug): {np_data}", xbmc.LOGDEBUG)
                        if isinstance(np_data, list) and len(np_data) > 0:
                            full_title = np_data[0].get('title', '')
                            if full_title:
                                artist, title = _parse_radiode_api_title(full_title, station_name)
                                if artist or title:
                                    xbmc.log(f"[{ADDON_NAME}] ✓ now-playing via Slug: {artist} - {title}", xbmc.LOGINFO)
                                    return artist, title
                        xbmc.log(f"[{ADDON_NAME}] ✗ Slug-Abfrage ohne Ergebnis – weiter mit Suche", xbmc.LOGDEBUG)
                except Exception as e:
                    xbmc.log(f"[{ADDON_NAME}] Fehler bei now-playing via Slug: {e}", xbmc.LOGDEBUG)

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
            
            xbmc.log(f"[{ADDON_NAME}] Suche radio.de API mit: '{search_name}' (Original: '{station_name}')", xbmc.LOGDEBUG)
            
            params = {'query': search_name, 'count': 20}
            response = requests.get(RADIODE_SEARCH_API_URL, params=params, headers=DEFAULT_HTTP_HEADERS, timeout=5)
            if response.status_code != 200:
                xbmc.log(f"[{ADDON_NAME}] radio.de API: ungültige Antwort (Status {response.status_code})", xbmc.LOGWARNING)
                return None, None
            data = response.json()
            
            xbmc.log(f"[{ADDON_NAME}] Search API: {data.get('totalCount', 0)} Treffer", xbmc.LOGDEBUG)
            
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
                        xbmc.log(f"[{ADDON_NAME}] EXAKTER MATCH gefunden: '{station_found}'", xbmc.LOGDEBUG)
                        break
                    
                    # Substring-Match (Station enthält Suchbegriff)
                    if search_normalized in station_normalized:
                        score = 100 + len(search_normalized)  # Je länger der Match, desto besser
                        if score > best_match_score:
                            best_match = station
                            best_match_score = score
                            xbmc.log(f"[{ADDON_NAME}] Substring-Match: '{station_found}' - Score: {score}", xbmc.LOGDEBUG)
                    
                    # Wort-basierter Match
                    elif search_normalized:
                        search_words = set(search_normalized.split())
                        station_words = set(station_normalized.split())
                        matching_words = search_words.intersection(station_words)
                        score = len(matching_words) * 10
                        
                        if score > best_match_score:
                            best_match = station
                            best_match_score = score
                            xbmc.log(f"[{ADDON_NAME}] Wort-Match: '{station_found}' - Score: {score} (Woerter: {matching_words})", xbmc.LOGDEBUG)
                
                if best_match and best_match_score > 0:
                    station_found = best_match.get('name', '')
                    station_id = best_match.get('id', '')
                    station_logo = best_match.get('logo300x300', '')  # Logo aus API

                    # Speichere Logo für spätere Verwendung
                    if station_logo:
                        self.station_logo = station_logo
                        self.set_logo_safe()
                        xbmc.log(f"[{ADDON_NAME}] Station-Logo aus API: {station_logo}", xbmc.LOGINFO)

                    xbmc.log(f"[{ADDON_NAME}] Beste Uebereinstimmung: '{station_found}' (Score: {best_match_score}, ID: {station_id})", xbmc.LOGDEBUG)
                    
                    # Schritt 2: Station-ID für now-playing API verwenden
                    if station_id:
                        xbmc.log(f"[{ADDON_NAME}] Hole Now-Playing von: {RADIODE_NOWPLAYING_API_URL}?stationIds={station_id}", xbmc.LOGDEBUG)
                        
                        try:
                            params = {'stationIds': station_id}
                            np_response = requests.get(RADIODE_NOWPLAYING_API_URL, params=params, headers=DEFAULT_HTTP_HEADERS, timeout=5)
                            if np_response.status_code == 200:
                                np_data = np_response.json()
                                xbmc.log(f"[{ADDON_NAME}] now-playing API Response: {np_data}", xbmc.LOGDEBUG)
                                
                                # Response ist ein Array: [{"title":"ARTIST - TITLE","stationId":"..."}]
                                if isinstance(np_data, list) and len(np_data) > 0:
                                    track_info = np_data[0]
                                    full_title = track_info.get('title', '')
                                    
                                    xbmc.log(f"[{ADDON_NAME}] Empfangener Titel: '{full_title}'", xbmc.LOGDEBUG)
                                    
                                    if full_title:
                                        artist, title = _parse_radiode_api_title(full_title, station_name)
                                        if artist is not None or title is not None:
                                            if artist and title:
                                                xbmc.log(f"[{ADDON_NAME}] ✓ now-playing API erfolgreich: {artist} - {title}", xbmc.LOGINFO)
                                                return artist, title
                                            if title:
                                                xbmc.log(f"[{ADDON_NAME}] ✓ now-playing API erfolgreich (nur Title): {title}", xbmc.LOGINFO)
                                                return None, title
                                    else:
                                        xbmc.log(f"[{ADDON_NAME}] ✗ Titel-Format unbekannt: '{full_title}'", xbmc.LOGDEBUG)
                                else:
                                    xbmc.log(f"[{ADDON_NAME}] ✗ Leere now-playing Response", xbmc.LOGDEBUG)
                            else:
                                xbmc.log(f"[{ADDON_NAME}] ✗ now-playing API Fehler: {np_response.status_code}", xbmc.LOGDEBUG)
                        except Exception as e:
                            xbmc.log(f"[{ADDON_NAME}] Fehler bei now-playing API: {str(e)}", xbmc.LOGWARNING)

                    else:
                        xbmc.log(f"[{ADDON_NAME}] ✗ Keine Station-ID gefunden", xbmc.LOGDEBUG)
                else:
                    xbmc.log(f"[{ADDON_NAME}] ✗ Kein Match gefunden (Score zu niedrig)", xbmc.LOGDEBUG)
            else:
                xbmc.log(f"[{ADDON_NAME}] ✗ Keine Treffer für '{search_name}'", xbmc.LOGDEBUG)
                        
        except Exception as e:
            xbmc.log(f"[{ADDON_NAME}] Fehler bei radio.de API Abfrage: {str(e)}", xbmc.LOGWARNING)
        
        return None, None
    
    def api_metadata_worker(self, generation):
        """Fallback: Pollt verschiedene APIs wenn keine ICY-Metadaten verfügbar"""
        if not self._is_api_source_allowed():
            self._log_api_source_blocked('api_metadata_worker_start')
            return

        xbmc.log(f"[{ADDON_NAME}] API Metadata Worker gestartet (Fallback-Modus)", xbmc.LOGDEBUG)

        # Timer-Status beim Start des Workers sauber initialisieren.
        # Verhindert, dass ein alter Timer aus einem vorherigen Stream sofort greift.
        self._reset_song_timeout_state(clear_debug=True)

        last_title = ""
        poll_interval = 10  # Sekunden zwischen API-Abfragen
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
                self._update_timeout_remaining_property()
                if self._last_song_time and time.time() - self._last_song_time > self._song_timeout:
                    xbmc.log(
                        f"[{ADDON_NAME}] Song-Timeout abgelaufen ({self._song_timeout:.0f}s) – lösche Song-Properties",
                        xbmc.LOGDEBUG
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
            xbmc.log(f"[{ADDON_NAME}] Fehler im API Metadata Worker: {str(e)}", xbmc.LOGERROR)
        finally:
            xbmc.log(f"[{ADDON_NAME}] API Metadata Worker beendet", xbmc.LOGDEBUG)

    def _musicplayer_metadata_fallback(self, generation):
        """Fallback für Streams ohne ICY und ohne radio.de-API.
        Pollt MusicPlayer.Artist/Title auf Änderungen (wie api_metadata_worker die radio.de API).
        Titelwechsel auf Live-Streams (z.B. Mother Earth Radio) werden so erkannt.
        Für Library-Streams (Ampache) beendet sich die Schleife beim nächsten onAVStarted
        automatisch via metadata_generation-Check.
        Wenn MusicPlayer leer bleibt, wird RadioMonitor.Playing gecleart.
        Station wird nicht gesetzt – es gibt keinen ICY-Stationsnamen.
        """
        xbmc.log(f"[{ADDON_NAME}] MusicPlayer-Fallback aktiv (kein ICY, kein API-Fallback)", xbmc.LOGDEBUG)

        last_artist = ''
        last_title = ''
        poll_interval = 5  # Sekunden zwischen MusicPlayer-Checks

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
                        xbmc.log(f"[{ADDON_NAME}] MusicPlayer-Fallback: kein Audio - deaktiviere RadioMonitor", xbmc.LOGDEBUG)
                        return
                    info_tag = self.player.getMusicInfoTag()
                    mp_artist = (info_tag.getArtist() or '').strip()
                    mp_title = (info_tag.getTitle() or '').strip()
                except Exception as e:
                    xbmc.log(f"[{ADDON_NAME}] MusicPlayer-Fallback: Fehler beim Lesen der Metadaten: {e}", xbmc.LOGDEBUG)
                    WINDOW.clearProperty(_P.PLAYING)
                    return

                # Erster Durchlauf und beide leer → deaktivieren
                if not last_artist and not last_title and not mp_artist and not mp_title:
                    xbmc.log(f"[{ADDON_NAME}] MusicPlayer-Fallback: Artist und Title leer - deaktiviere RadioMonitor", xbmc.LOGDEBUG)
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
                        xbmc.log(f"[{ADDON_NAME}] MusicPlayer-Fallback: kein Artist ermittelbar - deaktiviere RadioMonitor", xbmc.LOGDEBUG)
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
                self._update_timeout_remaining_property()
                if self._last_song_time and time.time() - self._last_song_time > self._song_timeout:
                    xbmc.log(
                        f"[{ADDON_NAME}] Song-Timeout abgelaufen ({self._song_timeout:.0f}s) – lösche Song-Properties",
                        xbmc.LOGDEBUG
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

                # Warte vor nächster Abfrage (in 0.5s-Schritten für schnelles Beenden)
                for _ in range(poll_interval * 2):
                    if (
                        self.stop_thread
                        or not self.is_playing
                        or generation != self.metadata_generation
                    ):
                        break
                    time.sleep(0.5)

        finally:
            xbmc.log(f"[{ADDON_NAME}] MusicPlayer-Fallback beendet", xbmc.LOGDEBUG)

    def parse_icy_metadata(self, url):
        """Liest ICY-Metadaten aus dem Stream"""
        try:
            headers = {'Icy-MetaData': '1', **DEFAULT_HTTP_HEADERS}
            response = requests.get(url, headers=headers, stream=True, timeout=5)
            
            # KOMPLETT LOGGEN: Alle ICY-Header
            xbmc.log(f"[{ADDON_NAME}] === ALLE ICY RESPONSE HEADERS ===", xbmc.LOGDEBUG)
            for header_name, header_value in response.headers.items():
                if 'icy' in header_name.lower() or 'ice' in header_name.lower():
                    xbmc.log(f"[{ADDON_NAME}]   {header_name}: {header_value}", xbmc.LOGDEBUG)
            xbmc.log(f"[{ADDON_NAME}] =================================", xbmc.LOGDEBUG)
            
            # ICY-Metadaten aus den Headers
            icy_name = response.headers.get('icy-name', '')
            icy_genre = response.headers.get('icy-genre', '')
            
            # Station initial aus ICY-Header icy-name (wird von API überschrieben falls verfügbar)
            station_name = icy_name
            if station_name:
                xbmc.log(f"[{ADDON_NAME}] Station (ICY): {station_name}", xbmc.LOGDEBUG)
            else:
                xbmc.log(f"[{ADDON_NAME}] Kein icy-name im Header", xbmc.LOGDEBUG)
            
            if icy_genre:
                xbmc.log(f"[{ADDON_NAME}] Genre: {icy_genre}", xbmc.LOGDEBUG)
            
            # Metaint - Position der Metadaten im Stream
            metaint = response.headers.get('icy-metaint')
            if not metaint:
                xbmc.log(f"[{ADDON_NAME}] Kein icy-metaint Header gefunden - Stream sendet keine ICY-Metadaten", xbmc.LOGWARNING)
                self._setup_api_fallback_from_url(url)
                response.close()
                return None

            metaint = int(metaint)
            xbmc.log(f"[{ADDON_NAME}] MetaInt: {metaint}", xbmc.LOGDEBUG)
            
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
        self._set_last_song_decision('', None, None)
        invalid = INVALID_METADATA_VALUES + ["", station_name]
        artist, title, is_von, has_multi = _parse_metadata_complex(stream_title, station_name)

        # --- Kandidaten sammeln ---
        candidates = []
        api_candidate = (None, None)
        api_changed = False
        mp_direct = (None, None)
        mp_swapped = (None, None)
        mp_pairs = []

        # MusicPlayer-Kandidaten lesen; Nutzung fuer die Entscheidung nur bei bestaetigtem Trust.
        mp_direct, mp_swapped = self._read_musicplayer_candidates(invalid)
        mp_pairs = self._valid_song_pairs(mp_direct, mp_swapped)
        if mp_pairs and self._is_musicplayer_trusted():
            self._mp_mismatch_count = 0
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
                        f"[{ADDON_NAME}] API-Kandidat geblockt nach Timeout: '{api_artist} - {api_title}'",
                        xbmc.LOGDEBUG
                    )
                else:
                    if self._api_timeout_block_key != ('', '') and api_key != self._api_timeout_block_key:
                        xbmc.log(
                            f"[{ADDON_NAME}] API-Song geändert, Timeout-Block aufgehoben: '{api_artist} - {api_title}'",
                            xbmc.LOGINFO
                        )
                        self._api_timeout_block_key = ('', '')
                    candidates.append({'source': 'api', 'artist': api_artist, 'title': api_title})
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
        mp_candidates_allowed = self._should_use_musicplayer_candidates(
            mp_pairs,
            api_candidate,
            icy_candidate_pairs
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

        winner, evaluations = self._select_mb_winner(candidates)
        if winner:
            # Für source-locked Trigger immer das source-native Eingangspaar speichern.
            # MB-normalisierte Namen können sich vom API/ICY/MP-Original unterscheiden
            # und sonst fälschlich als "Quelle gewechselt" wirken.
            self._set_last_song_decision(
                winner.get('source'),
                winner.get('input_artist'),
                winner.get('input_title')
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

        # Sonderfall B: MB kann zwischen Kandidaten nicht entscheiden (alle score=0).
        # Dann API nur übernehmen, wenn sie sich gegenüber der letzten API-Antwort geändert hat.
        # Sonst gilt: keine verlässlichen Songdaten -> Artist/Title leer lassen.
        if evaluations and all(ev.get('score', 0) == 0 for ev in evaluations):
            # MusicPlayer-Konsens: wenn MusicPlayer (direkt/swapped) mit API oder ICY übereinstimmt,
            # übernehme MusicPlayer auch ohne MB-Treffer.
            # Bootstrap: gilt auch wenn MP bisher untrusted war.
            icy_pairs = {
                (ev.get('input_artist'), ev.get('input_title'))
                for ev in evaluations
                if str(ev.get('source', '')).startswith('icy')
            }
            locked_source_family, locked_source_pair = self._resolve_mb_zero_with_source_lock(
                locked_source,
                mp_pairs,
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
                self._update_musicplayer_trust_after_decision(
                    locked_source_family,
                    locked_source_pair,
                    mp_pairs
                )
                return locked_source_pair[0], locked_source_pair[1], '', '', '', '', 0

            for mp_pair in mp_pairs:
                if mp_pair == api_candidate or mp_pair in icy_pairs:
                    if not self._is_musicplayer_trusted():
                        self._mark_musicplayer_trusted(
                            f"Bootstrap bei MB=0: '{mp_pair[0]} - {mp_pair[1]}'"
                        )
                    xbmc.log(
                        f"[{ADDON_NAME}] MB score=0 für alle Kandidaten, MusicPlayer konsistent -> nutze MusicPlayer: "
                        f"'{mp_pair[0]} - {mp_pair[1]}'",
                        xbmc.LOGINFO
                    )
                    self._set_last_song_decision('musicplayer', mp_pair[0], mp_pair[1])
                    self._update_musicplayer_trust_after_decision('musicplayer', mp_pair, mp_pairs)
                    return mp_pair[0], mp_pair[1], '', '', '', '', 0

            has_icy_candidate = any(str(ev.get('source', '')).startswith('icy') for ev in evaluations)
            if api_candidate[0] and api_candidate[1] and (api_changed or not has_icy_candidate):
                reason = "API hat gewechselt" if api_changed else "kein valider ICY-Kandidat"
                xbmc.log(
                    f"[{ADDON_NAME}] MB score=0 für alle Kandidaten, {reason} -> nutze API: "
                    f"'{api_candidate[0]} - {api_candidate[1]}'",
                    xbmc.LOGINFO
                )
                self._set_last_song_decision('api', api_candidate[0], api_candidate[1])
                self._update_musicplayer_trust_after_decision('api', api_candidate, mp_pairs)
                return api_candidate[0], api_candidate[1], '', '', '', '', 0
            xbmc.log(
                f"[{ADDON_NAME}] MB score=0 für alle Kandidaten, keine belastbaren Songdaten -> "
                f"nutze nur Station/StreamTitle",
                xbmc.LOGDEBUG
            )
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
        self._update_musicplayer_trust_after_decision('icy', (mb_artist, mb_title), mp_pairs)
        return mb_artist, mb_title, mb_album, mb_album_date, mbid, mb_first_release, duration_ms
        
    def metadata_worker(self, url, generation):
        """Worker-Thread zum kontinuierlichen Auslesen der Metadaten"""
        xbmc.log(f"[{ADDON_NAME}] Metadata Worker gestartet", xbmc.LOGDEBUG)

        # Timer-Status beim Start des Workers sauber initialisieren.
        # Gilt auch für den No-ICY-Pfad (API/MusicPlayer-Fallback).
        self._reset_song_timeout_state(clear_debug=True)

        stream_info = self.parse_icy_metadata(url)
        if not stream_info:
            xbmc.log(f"[{ADDON_NAME}] Keine ICY-Metadaten verfuegbar - wechsle zu Fallback", xbmc.LOGWARNING)
            if not self._is_api_source_allowed():
                self._log_api_source_blocked('metadata_worker_no_icy')
            if (
                self._is_api_source_allowed()
                and (self.use_api_fallback or self.plugin_slug or self.tunein_station_id)
                and generation == self.metadata_generation
            ):
                self.api_metadata_worker(generation)
            elif generation == self.metadata_generation:
                self._musicplayer_metadata_fallback(generation)
            return
            
        metaint = stream_info['metaint']
        response = stream_info.get('response')
        last_title = ""
        last_song_key = ('', '', '')
        last_winner_source = ''
        last_winner_pair = ('', '')
        initial_source_pending = False
        startup_stable_confirmed = False
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
                    
                    if meta_length > 0 or last_winner_source.startswith('musicplayer'):
                        if meta_length > 0:
                            # Metadaten lesen
                            metadata = response.raw.read(meta_length)
                            if generation != self.metadata_generation:
                                break
                            metadata_str = metadata.decode('utf-8', errors='ignore').strip('\x00')

                            # KOMPLETT LOGGEN: Rohe ICY-Metadaten
                            if metadata_str:
                                xbmc.log(f"[{ADDON_NAME}] === ICY METADATA (ROH) ===", xbmc.LOGDEBUG)
                                xbmc.log(f"[{ADDON_NAME}] {metadata_str}", xbmc.LOGDEBUG)
                                xbmc.log(f"[{ADDON_NAME}] =========================", xbmc.LOGDEBUG)

                            stream_title = self.extract_stream_title(metadata_str)
                        else:
                            # Bei aktivem MusicPlayer-Lock auch ohne neuen ICY-Block
                            # MP-Aenderungen erkennen (z.B. wenn Sender selten ICY schreibt).
                            stream_title = last_title

                        station_name = stream_info.get('station', '')
                        invalid_values = INVALID_METADATA_VALUES + ["", station_name]
                        mp_direct_live, mp_swapped_live = self._read_musicplayer_candidates(invalid_values)
                        mp_live_pairs = self._valid_song_pairs(mp_direct_live, mp_swapped_live)
                        current_mp_pair = mp_live_pairs[0] if mp_live_pairs else ('', '')

                        # API-Daten erst nach stabilem Start oder nach gesetzter Erstquelle aktualisieren.
                        # Dadurch wird waehrend sichtbarem Kodi-Buffering kein API-Property vorbefuellt.
                        api_refresh_allowed = startup_stable_confirmed or bool(last_winner_source)
                        if api_refresh_allowed:
                            self._refresh_api_data_property(station_name)
                            current_api_pair = self._latest_api_pair
                        else:
                            self._latest_api_pair = ('', '')
                            current_api_pair = ('', '')

                        # StreamTitle unabhängig vom Gewinner aktuell halten.
                        stream_title_changed = (stream_title != last_title)
                        if stream_title_changed:
                            last_title = stream_title
                            xbmc.log(f"[{ADDON_NAME}] Neuer StreamTitle erkannt: '{stream_title}'", xbmc.LOGDEBUG)

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
                                # Nach dem Wait im selben Durchlauf die Initial-Entscheidung erzwingen.
                                initial_source_pending = True
                                # MP/API nach dem Wait neu lesen, damit die Quellenwahl
                                # den tatsaechlich stabilen Startzustand verwendet.
                                mp_direct_live, mp_swapped_live = self._read_musicplayer_candidates(invalid_values)
                                mp_live_pairs = self._valid_song_pairs(mp_direct_live, mp_swapped_live)
                                current_mp_pair = mp_live_pairs[0] if mp_live_pairs else ('', '')
                                self._refresh_api_data_property(station_name)
                                current_api_pair = self._latest_api_pair
                                if station_name:
                                    self.set_property_safe(_P.STATION, station_name)
                                if stream_info.get('genre'):
                                    self.set_property_safe(_P.GENRE, stream_info.get('genre'))

                        # Source-locked Trigger: nur die letzte Gewinnerquelle entscheidet den Wechsel-Trigger.
                        source_changed_trigger, trigger_reason = self._determine_source_change_trigger(
                            last_winner_source,
                            last_winner_pair,
                            current_mp_pair,
                            current_api_pair,
                            stream_title_changed,
                            initial_source_pending
                        )

                        if source_changed_trigger:
                            # Bei Trigger: MusicBrainz-Cache invalidieren
                            try:
                                _mb_cache.clear()
                                xbmc.log(f"[{ADDON_NAME}] MB-Cache invalidiert wegen {trigger_reason}", xbmc.LOGDEBUG)
                            except Exception:
                                pass
                            if trigger_reason.startswith('MusicPlayer'):
                                xbmc.log(
                                    f"[{ADDON_NAME}] MusicPlayer-Titelwechsel erkannt (trusted): "
                                    f"'{current_mp_pair[0]} - {current_mp_pair[1]}'",
                                    xbmc.LOGDEBUG
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
                            if trigger_reason == self.TRIGGER_MP_CHANGE:
                                parse_locked_source = 'musicplayer'
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
                            current_song_key = (artist or '', title or '', mbid or '')
                            is_new_song = (current_song_key != last_song_key)

                            # Wenn beide None sind (z.B. bei Zahlen-IDs ohne API-Daten), überspringe diesen Titel
                            if artist is None and title is None:
                                xbmc.log(f"[{ADDON_NAME}] Keine verwertbaren Metadaten fuer '{stream_title}' - RadioMonitor Properties bleiben leer", xbmc.LOGDEBUG)
                                # Bei klar fehlenden Songdaten: nur Song-Properties löschen,
                                # Station + StreamTitle bleiben gesetzt.
                                if stream_title and stream_title not in INVALID_METADATA_VALUES:
                                    self.set_property_safe(_P.STREAM_TTL, stream_title)
                                else:
                                    WINDOW.clearProperty(_P.STREAM_TTL)
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
                                last_winner_source = ''
                                last_winner_pair = ('', '')
                                continue
                            
                            if stream_title not in INVALID_METADATA_VALUES:
                                self.set_property_safe(_P.STREAM_TTL, stream_title)
                            
                            # Reihenfolge: Title und MBID vor Artist setzen.
                            # AS lauscht auf RadioMonitor.Artist als Trigger und liest
                            # danach sofort RadioMonitor.MBID – daher muss MBID bereits
                            # gesetzt sein wenn Artist den Trigger auslöst.
                            if title:
                                self.set_property_safe(_P.TITLE, title)
                                xbmc.log(f"[{ADDON_NAME}] Title: {title}", xbmc.LOGDEBUG)
                            else:
                                WINDOW.clearProperty(_P.TITLE)
                                title = ''
                            if album:
                                self.set_property_safe(_P.ALBUM, album)
                                xbmc.log(f"[{ADDON_NAME}] Album: {album}", xbmc.LOGDEBUG)
                            else:
                                WINDOW.clearProperty(_P.ALBUM)
                            if album_date:
                                self.set_property_safe(_P.ALBUM_DATE, album_date)
                                xbmc.log(f"[{ADDON_NAME}] AlbumDate: {album_date}", xbmc.LOGDEBUG)
                            else:
                                WINDOW.clearProperty(_P.ALBUM_DATE)
                            if mbid:
                                self.set_property_safe(_P.MBID, mbid)
                                xbmc.log(f"[{ADDON_NAME}] MBID: {mbid}", xbmc.LOGDEBUG)
                            else:
                                WINDOW.clearProperty(_P.MBID)
                            if first_release:
                                self.set_property_safe(_P.FIRST_REL, first_release)
                                xbmc.log(f"[{ADDON_NAME}] FirstRelease: {first_release}", xbmc.LOGDEBUG)
                            else:
                                WINDOW.clearProperty(_P.FIRST_REL)
                            if artist:
                                self.set_property_safe(_P.ARTIST, artist)
                                xbmc.log(f"[{ADDON_NAME}] Artist: {artist}", xbmc.LOGDEBUG)
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
                                    xbmc.log(f"[{ADDON_NAME}] BandFormed: {band_formed}", xbmc.LOGDEBUG)
                                else:
                                    WINDOW.clearProperty(_P.BAND_FORM)
                                if band_members:
                                    self.set_property_safe(_P.BAND_MEM, band_members)
                                    xbmc.log(f"[{ADDON_NAME}] BandMembers: {band_members}", xbmc.LOGDEBUG)
                                else:
                                    WINDOW.clearProperty(_P.BAND_MEM)
                                if mb_genre:
                                    self.set_property_safe(_P.GENRE, mb_genre)
                                    xbmc.log(f"[{ADDON_NAME}] Genre (MB): {mb_genre}", xbmc.LOGDEBUG)
                            else:
                                WINDOW.clearProperty(_P.BAND_FORM)
                                WINDOW.clearProperty(_P.BAND_MEM)
                            
                            # DEBUG: Zeige alle gesetzten Properties
                            xbmc.log(f"[{ADDON_NAME}] === PROPERTIES GESETZT ===", xbmc.LOGDEBUG)
                            xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Playing = {WINDOW.getProperty(_P.PLAYING)}", xbmc.LOGDEBUG)
                            xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Station = {WINDOW.getProperty(_P.STATION)}", xbmc.LOGDEBUG)
                            xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Artist = {WINDOW.getProperty(_P.ARTIST)}", xbmc.LOGDEBUG)
                            xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Title = {WINDOW.getProperty(_P.TITLE)}", xbmc.LOGDEBUG)
                            xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Album = {WINDOW.getProperty(_P.ALBUM)}", xbmc.LOGDEBUG)
                            xbmc.log(f"[{ADDON_NAME}] RadioMonitor.AlbumDate = {WINDOW.getProperty(_P.ALBUM_DATE)}", xbmc.LOGDEBUG)
                            xbmc.log(f"[{ADDON_NAME}] RadioMonitor.MBID = {WINDOW.getProperty(_P.MBID)}", xbmc.LOGDEBUG)
                            xbmc.log(f"[{ADDON_NAME}] RadioMonitor.FirstRelease = {WINDOW.getProperty(_P.FIRST_REL)}", xbmc.LOGDEBUG)
                            xbmc.log(f"[{ADDON_NAME}] RadioMonitor.BandFormed = {WINDOW.getProperty(_P.BAND_FORM)}", xbmc.LOGDEBUG)
                            xbmc.log(f"[{ADDON_NAME}] RadioMonitor.BandMembers = {WINDOW.getProperty(_P.BAND_MEM)}", xbmc.LOGDEBUG)
                            xbmc.log(f"[{ADDON_NAME}] RadioMonitor.StreamTitle = {WINDOW.getProperty(_P.STREAM_TTL)}", xbmc.LOGDEBUG)
                            xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Genre = {WINDOW.getProperty(_P.GENRE)}", xbmc.LOGDEBUG)
                            xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Logo = {WINDOW.getProperty(_P.LOGO)}", xbmc.LOGDEBUG)

                            # Aktualisiere Kodi Player Metadaten (fuer Standard InfoLabels)
                            winner_source_for_player = str(decision_source or last_winner_source or '')
                            if winner_source_for_player.startswith('musicplayer'):
                                xbmc.log(
                                    f"[{ADDON_NAME}] Player InfoTag Update uebersprungen "
                                    f"(Quelle=musicplayer)",
                                    xbmc.LOGDEBUG
                                )
                            else:
                                logo = WINDOW.getProperty(_P.LOGO)
                                self.update_player_metadata(artist if artist else None,
                                                            title if title else None,
                                                            album if album else station_name,
                                                            logo if logo else None,
                                                            mbid if mbid else None)
                            
                            # DEBUG: Zeige was Kodi Player hat
                            try:
                                if self.player.isPlayingAudio():
                                    info_tag = self.player.getMusicInfoTag()
                                    xbmc.log(f"[{ADDON_NAME}] === KODI PLAYER INFOTAGS ===", xbmc.LOGDEBUG)
                                    xbmc.log(f"[{ADDON_NAME}] MusicPlayer.Artist = {info_tag.getArtist()}", xbmc.LOGDEBUG)
                                    xbmc.log(f"[{ADDON_NAME}] MusicPlayer.Title = {info_tag.getTitle()}", xbmc.LOGDEBUG)
                                    xbmc.log(f"[{ADDON_NAME}] MusicPlayer.Album = {info_tag.getAlbum()}", xbmc.LOGDEBUG)
                            except Exception as e:
                                xbmc.log(f"[{ADDON_NAME}] Fehler beim Lesen Player InfoTags: {str(e)}", xbmc.LOGDEBUG)
                            
                            xbmc.log(f"[{ADDON_NAME}] ========================", xbmc.LOGDEBUG)
                            
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
                                xbmc.log(f"[{ADDON_NAME}] Fehler bei JSON-RPC Notify: {str(e)}", xbmc.LOGDEBUG)
                            
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

                    # Song-Timeout Anzeige aktualisieren und ggf. Properties loeschen.
                    self._update_timeout_remaining_property()
                    if startup_stable_confirmed or last_winner_source:
                        self._refresh_api_data_property(stream_info.get('station', ''))
                    # Läuft jede Iteration (~1s) – kein extra Thread notwendig.
                    if self._last_song_time and time.time() - self._last_song_time > self._song_timeout:
                        xbmc.log(
                            f"[{ADDON_NAME}] Song-Timeout abgelaufen ({self._song_timeout:.0f}s) – lösche Song-Properties",
                            xbmc.LOGDEBUG
                        )
                        if last_song_key[0] and last_song_key[1]:
                            self._api_timeout_block_key = (last_song_key[0], last_song_key[1])
                            xbmc.log(
                                f"[{ADDON_NAME}] API-Block bis Songwechsel aktiviert: "
                                f"'{last_song_key[0]} - {last_song_key[1]}'",
                                xbmc.LOGDEBUG
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
                        self._reset_song_timeout_state(clear_debug=True)  # Verhindert wiederholtes Löschen

                except Exception as e:
                    xbmc.log(f"[{ADDON_NAME}] Fehler im Metadata-Loop (Thread läuft weiter): {str(e)}", xbmc.LOGERROR)
                    time.sleep(1)
                    continue

        except Exception as e:
            xbmc.log(f"[{ADDON_NAME}] Fehler im Metadata Worker: {str(e)}", xbmc.LOGERROR)
        finally:
            try:
                if response is not None:
                    response.close()
            except Exception as e:
                xbmc.log(f"[{ADDON_NAME}] Stream-Response konnte nicht geschlossen werden: {e}", xbmc.LOGDEBUG)
            xbmc.log(f"[{ADDON_NAME}] Metadata Worker beendet", xbmc.LOGDEBUG)
            
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
                        xbmc.log(f"[{ADDON_NAME}] Video läuft - kein Radio-Monitoring", xbmc.LOGDEBUG)
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
                        self._ensure_api_source_from_context(playing_file, 'check_playing_new_url')
                        if self._can_use_tunein_api() and not self.tunein_station_id:
                            tunein_id = self._extract_tunein_station_id(playing_file)
                            if tunein_id:
                                self.tunein_station_id = tunein_id
                                xbmc.log(f"[{ADDON_NAME}] TuneIn Station-ID aus Stream-URL: '{tunein_id}'", xbmc.LOGDEBUG)
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
                            
                            # 1. HÖCHSTE Priorität: ListItem.Icon (echtes Logo vom Addon, BEVOR Kodi es cached)
                            listitem_icon = xbmc.getInfoLabel('ListItem.Icon')
                            if self.is_real_logo(listitem_icon):
                                logo = listitem_icon
                                self.station_logo = logo
                                self._ensure_api_source_from_context(logo, 'check_playing_listitem_logo')
                                xbmc.log(f"[{ADDON_NAME}] Logo vom ListItem.Icon: {logo}", xbmc.LOGINFO)
                            
                            # 2. Fallback: Window-Property vom radio.de Addon
                            if not logo:
                                radiode_logo = WINDOW.getProperty('RadioDE.StationLogo')
                                if self.is_real_logo(radiode_logo):
                                    logo = radiode_logo
                                    self.station_logo = logo
                                    self._ensure_api_source_from_context(logo, 'check_playing_radiode_logo')
                                    xbmc.log(f"[{ADDON_NAME}] Logo vom radio.de Addon (Window-Property): {logo}", xbmc.LOGINFO)
                            
                            # 3. Fallback: Player Art
                            if not logo:
                                for source in ['Player.Art(poster)', 'Player.Icon', 'Player.Art(thumb)', 'MusicPlayer.Cover']:
                                    player_logo = xbmc.getInfoLabel(source)
                                    if self.is_real_logo(player_logo):
                                        logo = player_logo
                                        self.station_logo = logo
                                        self._ensure_api_source_from_context(logo, f'check_playing_{source}')
                                        xbmc.log(f"[{ADDON_NAME}] Logo von {source}: {logo}", xbmc.LOGINFO)
                                        break

                            if not self.station_logo or not self.is_real_logo(self.station_logo):
                                xbmc.log(f"[{ADDON_NAME}] Kein Player-Logo, wird spaeter von API geholt", xbmc.LOGDEBUG)
                            
                            # Nur Title als vorläufige Info setzen.
                            # RadioMonitor.Artist und RadioMonitor.Album werden bewusst NICHT gesetzt.
                            # Artist ist der Trigger für AS, und ohne MBID würde AS mit falschen Daten starten.
                            # Album wird erst nach erfolgreicher MB-Query gesetzt.
                            # Artist, Album + MBID werden zusammen vom Metadata-Worker gesetzt.
                            if title:
                                self.set_property_safe(_P.TITLE, title)
                            
                            # Setze Logo (nur wenn echtes Logo, sonst Kodi-Fallback)
                            self.set_logo_safe()
                            if self.station_logo and self.is_real_logo(self.station_logo):
                                xbmc.log(f"[{ADDON_NAME}] Logo gesetzt: {self.station_logo}", xbmc.LOGINFO)
                            else:
                                xbmc.log(f"[{ADDON_NAME}] Kein echtes Logo, nutze Kodi-Fallback", xbmc.LOGDEBUG)
                        except Exception as e:
                            xbmc.log(f"[{ADDON_NAME}] Fehler beim Lesen von InfoTag/Logo beim Stream-Start: {e}", xbmc.LOGDEBUG)
                        if album and (not self.station_logo or self.station_logo == 'DefaultAudio.png'):
                            try:
                                xbmc.log(f"[{ADDON_NAME}] Hole Station-Logo für: {album}", xbmc.LOGDEBUG)
                                # Suche Station in radio.de API
                                search_name = album
                                search_name = re.sub(r'\s*(inter\d+|mp3|aac|low|high|128|64|256).*$', '', search_name, flags=re.IGNORECASE)
                                search_name = search_name.strip()
                                
                                params = {'query': search_name, 'count': 5}
                                response = requests.get(RADIODE_SEARCH_API_URL, params=params, headers=DEFAULT_HTTP_HEADERS, timeout=5)
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
                                xbmc.log(f"[{ADDON_NAME}] Fehler beim Holen des Station-Logos: {str(e)}", xbmc.LOGDEBUG)
                        
                        # Playing-Flag setzen
                        WINDOW.setProperty(_P.PLAYING, 'true')
                        
                        xbmc.log(f"[{ADDON_NAME}] === STREAM GESTARTET - INITIAL STATE ===", xbmc.LOGDEBUG)
                        xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Playing = true", xbmc.LOGDEBUG)
                        xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Station = {WINDOW.getProperty(_P.STATION)}", xbmc.LOGDEBUG)
                        xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Artist = {WINDOW.getProperty(_P.ARTIST)}", xbmc.LOGDEBUG)
                        xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Title = {WINDOW.getProperty(_P.TITLE)}", xbmc.LOGDEBUG)
                        xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Logo = {WINDOW.getProperty(_P.LOGO)}", xbmc.LOGDEBUG)
                        xbmc.log(f"[{ADDON_NAME}] RadioMonitor.Genre = {WINDOW.getProperty(_P.GENRE)}", xbmc.LOGDEBUG)
                        
                        # Zeige was vom Player kommt
                        try:
                            if self.player.isPlayingAudio():
                                info_tag = self.player.getMusicInfoTag()
                                xbmc.log(f"[{ADDON_NAME}] Initial MusicPlayer.Artist = {info_tag.getArtist()}", xbmc.LOGDEBUG)
                                xbmc.log(f"[{ADDON_NAME}] Initial MusicPlayer.Title = {info_tag.getTitle()}", xbmc.LOGDEBUG)
                                xbmc.log(f"[{ADDON_NAME}] Initial MusicPlayer.Album = {info_tag.getAlbum()}", xbmc.LOGDEBUG)
                        except Exception:
                            pass
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
        self.clear_properties()
        xbmc.log(f"[{ADDON_NAME}] Service beendet", xbmc.LOGINFO)

if __name__ == '__main__':
    monitor = RadioMonitor()
    monitor.run()
